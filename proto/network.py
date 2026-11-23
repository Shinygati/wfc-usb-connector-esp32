"""libslirp 4.9+ DHCP/DNS/TCP/UDP backend for native Windows x64.

Uses the public libslirp.h ABI. All callbacks and polls run on the AP event loop;
frames produced by the library are queued so USB calls never re-enter libslirp.
"""
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
import ctypes as C
import os
from pathlib import Path
import socket
import struct
import time
from dns_proxy import forward_query, short_reply

P = C.c_void_p
IPv6 = C.c_uint16 * 8  # Windows IN6_ADDR has USHORT alignment, not uint32 alignment.


class Config(C.Structure):
    _fields_ = [
        ('version', C.c_uint32), ('restricted', C.c_int), ('in_enabled', C.c_bool),
        ('vnetwork', C.c_uint32), ('vnetmask', C.c_uint32), ('vhost', C.c_uint32),
        ('in6_enabled', C.c_bool), ('vprefix_addr6', IPv6), ('vprefix_len', C.c_uint8),
        ('vhost6', IPv6), ('vhostname', C.c_char_p), ('tftp_server_name', C.c_char_p),
        ('tftp_path', C.c_char_p), ('bootfile', C.c_char_p), ('vdhcp_start', C.c_uint32),
        ('vnameserver', C.c_uint32), ('vnameserver6', IPv6), ('vdnssearch', P),
        ('vdomainname', C.c_char_p), ('if_mtu', C.c_size_t), ('if_mru', C.c_size_t),
        ('disable_host_loopback', C.c_bool), ('enable_emu', C.c_bool),
        ('outbound_addr', P), ('outbound_addr6', P), ('disable_dns', C.c_bool),
        ('disable_dhcp', C.c_bool), ('mfr_id', C.c_uint32), ('oob_eth_addr', C.c_uint8 * 6),
    ]


class Callbacks(C.Structure):
    _fields_ = [(name, P) for name in (
        'send_packet', 'guest_error', 'clock_get_ns', 'timer_new', 'timer_free',
        'timer_mod', 'register_poll_fd', 'unregister_poll_fd', 'notify',
        'init_completed', 'timer_new_opaque', 'register_poll_socket', 'unregister_poll_socket')]


class PollFD(C.Structure):
    _fields_ = [('fd', C.c_size_t), ('events', C.c_short), ('revents', C.c_short)]


def ipv4(value):
    return int.from_bytes(socket.inet_aton(value), 'little')


def checksum(data):
    if len(data) & 1:
        data += b'\0'
    value = sum(struct.unpack('!%dH' % (len(data) // 2), data))
    while value >> 16:
        value = (value & 65535) + (value >> 16)
    return (~value) & 65535


class SlirpNetwork:
    def __init__(self, output, logger=print, dll_dir=None, dns_upstream='system'):
        if os.name != 'nt' or C.sizeof(P) != 8:
            raise RuntimeError('this libslirp binding requires Windows x64')
        self.output, self.log = output, logger
        self.dns_upstream = None if dns_upstream == 'system' else socket.inet_aton(dns_upstream)
        self.dns_virtual = socket.inet_aton('192.168.77.2')
        self.resolver = ThreadPoolExecutor(max_workers=2, thread_name_prefix='wfc-dns')
        self.dns_jobs = []
        self.pending = deque(maxlen=256)
        self.counts = Counter()
        self.timers = {}
        self.next_timer = 0
        self.callbacks = []
        self.fds = []
        self.stack = None
        directory = Path(dll_dir or Path(__file__).resolve().parents[1] / 'runtime/ucrt64/bin').resolve()
        if not (directory / 'libslirp-0.dll').exists():
            raise RuntimeError('missing libslirp runtime: run python tools/fetch_slirp.py')
        self.dll_search = os.add_dll_directory(str(directory))
        self.lib = C.CDLL(str(directory / 'libslirp-0.dll'))
        self.lib.slirp_new.argtypes = [C.POINTER(Config), C.POINTER(Callbacks), P]
        self.lib.slirp_new.restype = P
        self.lib.slirp_cleanup.argtypes = [P]
        self.lib.slirp_input.argtypes = [P, C.c_char_p, C.c_int]
        self.lib.slirp_pollfds_fill_socket.argtypes = [P, C.POINTER(C.c_uint32), P, P]
        self.lib.slirp_pollfds_poll.argtypes = [P, C.c_int, P, P]
        self.lib.slirp_handle_timer.argtypes = [P, C.c_int, P]
        self.lib.slirp_version_string.restype = C.c_char_p
        self.winsock = C.WinDLL('ws2_32', use_last_error=True)
        self.winsock.WSAPoll.argtypes = [C.POINTER(PollFD), C.c_ulong, C.c_int]
        self.winsock.WSAPoll.restype = C.c_int
        self.cb = Callbacks()
        self.bind('send_packet', C.c_ssize_t, [P, C.c_size_t, P], self.send_packet)
        self.bind('guest_error', None, [C.c_char_p, P], lambda msg, _: self.log('SLIRP ' + msg.decode(errors='replace')))
        self.bind('clock_get_ns', C.c_int64, [P], lambda _: time.monotonic_ns())
        self.bind('timer_free', None, [P, P], lambda timer, _: self.timers.pop(timer, None))
        self.bind('timer_mod', None, [P, C.c_int64, P], self.timer_mod)
        self.bind('notify', None, [P], lambda _: None)
        self.bind('init_completed', None, [P, P], lambda stack, _: None)
        self.bind('timer_new_opaque', P, [C.c_int, P, P], self.timer_new)
        self.bind('register_poll_socket', None, [C.c_size_t, P], lambda fd, _: None)
        self.bind('unregister_poll_socket', None, [C.c_size_t, P], lambda fd, _: None)
        self.add_poll_cb = C.CFUNCTYPE(C.c_int, C.c_size_t, C.c_int, P)(self.add_poll)
        self.revents_cb = C.CFUNCTYPE(C.c_int, C.c_int, P)(self.revents)
        self.cfg = Config()
        self.cfg.version = 6
        self.cfg.in_enabled = True
        self.cfg.vnetwork = ipv4('192.168.77.0')
        self.cfg.vnetmask = ipv4('255.255.255.0')
        self.cfg.vhost = ipv4('192.168.77.1')
        self.cfg.vdhcp_start = ipv4('192.168.77.10')
        self.cfg.vnameserver = ipv4('192.168.77.2')
        self.cfg.vhostname = b'nintendo-usb'
        self.cfg.if_mtu = self.cfg.if_mru = 1500
        self.cfg.disable_host_loopback = True
        self.stack = self.lib.slirp_new(C.byref(self.cfg), C.byref(self.cb), None)
        if not self.stack:
            self.dll_search.close()
            raise RuntimeError('slirp_new failed')
        self.log('NETWORK libslirp=%s DHCP=192.168.77.10+ gateway=192.168.77.1 DNS=192.168.77.2' %
                 self.lib.slirp_version_string().decode())
        self.log('DNS upstream=' + dns_upstream)

    def bind(self, name, result, arguments, function):
        callback = C.CFUNCTYPE(result, *arguments)(function)
        self.callbacks.append(callback)
        setattr(self.cb, name, C.cast(callback, P))

    def send_packet(self, pointer, size, opaque):
        self.pending.append(C.string_at(pointer, size))
        return size

    def timer_new(self, kind, opaque, unused):
        self.next_timer += 1
        self.timers[self.next_timer] = [kind, opaque, None]
        return self.next_timer

    def timer_mod(self, timer, expiry, unused):
        self.timers[timer][2] = expiry

    def add_poll(self, fd, events, opaque):
        # Microsoft's WSAPoll provider rejects POLLPRI with WSAEINVAL. Asking
        # for it prevents *all* TCP reads; poll normal input/output only.
        flags = (0x100 if events & 1 else 0) | (0x10 if events & 2 else 0)
        self.fds.append(PollFD(fd, flags, 0))
        return len(self.fds) - 1

    def revents(self, index, opaque):
        value = self.poll_array[index].revents
        return ((1 if value & 0x300 else 0) | (2 if value & 0x10 else 0) |
                (4 if value & 0x400 else 0) | (8 if value & 5 else 0) | (16 if value & 2 else 0))

    def inspect(self, frame, direction):
        self.counts[direction] += 1
        if len(frame) >= 54 and frame[12:14] == b'\x08\x00' and frame[23] == 6:
            offset = 14 + (frame[14] & 15) * 4
            if offset < 34 or len(frame) < offset + 20:
                return
            self.counts['tcp_' + direction] += 1
            if self.counts['tcp_' + direction] <= 8:
                self.log('TCP %s flags=%02x bytes=%d' % (direction, frame[offset + 13], len(frame)))
        if len(frame) < 42 or frame[12:14] != b'\x08\x00' or frame[23] != 17:
            return
        offset = 14 + (frame[14] & 15) * 4
        if len(frame) < offset + 8:
            return
        src = int.from_bytes(frame[offset:offset + 2], 'big')
        dst = int.from_bytes(frame[offset + 2:offset + 4], 'big')
        if {src, dst} == {67, 68}:
            bootp = frame[offset + 8:]
            self.counts['dhcp_' + direction] += 1
            assigned = socket.inet_ntoa(bootp[16:20]) if len(bootp) >= 20 else '?'
            self.log('DHCP %s ip=%s' % (direction, assigned))
        elif 53 in (src, dst):
            self.counts['dns_' + direction] += 1
            dns = frame[offset + 8:]
            if len(dns) >= 12:
                self.log('DNS %s response=%d rcode=%d' %
                         (direction, bool(dns[2] & 128), dns[3] & 15))

    @staticmethod
    def resolve_query(query):
        if len(query) < 12 or query[2] & 0xf8 or query[4:6] != b'\0\x01':
            return None
        position, labels = 12, []
        while position < len(query) and query[position]:
            size = query[position]
            if size > 63 or position + 1 + size > len(query):
                return None
            labels.append(query[position + 1:position + 1 + size].decode('ascii', errors='strict'))
            position += size + 1
        position += 1
        if position + 4 > len(query):
            return None
        kind, cls = struct.unpack_from('!HH', query, position)
        question = query[12:position + 4]
        answers, status = [], 0
        if cls != 1 or kind not in (1, 28):
            status = 4
        else:
            family = socket.AF_INET if kind == 1 else socket.AF_INET6
            try:
                addresses = sorted({entry[4][0] for entry in socket.getaddrinfo('.'.join(labels), None, family, socket.SOCK_STREAM)})
                for address in addresses[:8]:
                    packed = socket.inet_pton(family, address)
                    answers.append(b'\xc0\x0c' + struct.pack('!HHIH', kind, 1, 60, len(packed)) + packed)
            except socket.gaierror as error:
                status = 3 if error.errno in (socket.EAI_NONAME, 11001) else 2
        flags = 0x8080 | (int.from_bytes(query[2:4], 'big') & 0x0100) | status
        return query[:2] + struct.pack('!HHHHH', flags, 1, len(answers), 0, 0) + question + b''.join(answers)

    def dns_input(self, frame):
        # Both DNS modes use bounded workers; USB polling never waits for DNS.
        # Keep DHCP and all other NAT traffic in libslirp.
        if len(frame) < 42 or frame[12:14] != b'\x08\x00' or frame[23] != 17:
            return False
        at = 14 + (frame[14] & 15) * 4
        if at < 34 or len(frame) < at + 8 or frame[30:34] != self.dns_virtual or frame[at + 2:at + 4] != b'\0\x35':
            return False
        size = int.from_bytes(frame[at + 4:at + 6], 'big')
        if size < 20 or len(frame) < at + size or int.from_bytes(frame[20:22], 'big') & 0x3fff:
            return True
        if len(self.dns_jobs) < 32:
            query = frame[at + 8:at + size]
            future = (self.resolver.submit(self.resolve_query, query) if self.dns_upstream is None else
                      self.resolver.submit(forward_query, query, socket.inet_ntoa(self.dns_upstream)))
            self.dns_jobs.append((frame, at, future))
        return True

    def poll_dns(self):
        waiting = []
        for frame, at, future in self.dns_jobs:
            if not future.done():
                waiting.append((frame, at, future))
                continue
            try:
                reply = future.result()
            except (ValueError, UnicodeError, OSError) as exc:
                self.log('DNS upstream query failed: ' + str(exc))
                reply = None
                if self.dns_upstream is not None:
                    try:
                        size = int.from_bytes(frame[at + 4:at + 6], 'big')
                        reply = short_reply(frame[at + 8:at + size])
                    except ValueError:
                        pass
            if reply is None:
                continue
            udp = b'\0\x35' + frame[at:at + 2] + struct.pack('!HH', 8 + len(reply), 0) + reply
            ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0,
                             self.dns_virtual, frame[26:30])
            ip = ip[:10] + struct.pack('!H', checksum(ip)) + ip[12:]
            response = frame[6:12] + b'\x52\x55' + self.dns_virtual + b'\x08\x00' + ip + udp
            self.pending.append(response)
        self.dns_jobs = waiting

    def drain(self):
        while self.pending:
            frame = self.pending.popleft()
            self.inspect(frame, 'tx')
            self.output(frame)

    def input(self, ethernet):
        if not self.stack or not 14 <= len(ethernet) <= 1514:
            return
        self.inspect(ethernet, 'rx')
        if self.dns_input(ethernet):
            return
        self.lib.slirp_input(self.stack, ethernet, len(ethernet))
        self.drain()

    def poll(self):
        if not self.stack:
            return
        self.poll_dns()
        now = time.monotonic_ns() // 1000000
        for timer in list(self.timers.values()):
            if timer[2] is not None and timer[2] <= now:
                timer[2] = None
                self.lib.slirp_handle_timer(self.stack, timer[0], timer[1])
        self.fds = []
        timeout = C.c_uint32(0)
        self.lib.slirp_pollfds_fill_socket(self.stack, C.byref(timeout), self.add_poll_cb, None)
        self.poll_array = (PollFD * len(self.fds))(*self.fds)
        if self.fds and self.winsock.WSAPoll(self.poll_array, len(self.fds), 0) < 0:
            raise OSError(self.winsock.WSAGetLastError(), 'WSAPoll failed')
        self.lib.slirp_pollfds_poll(self.stack, 0, self.revents_cb, None)
        self.drain()

    def close(self):
        self.resolver.shutdown(wait=False, cancel_futures=True)
        if self.stack:
            self.lib.slirp_cleanup(self.stack)
            self.stack = None
            self.dll_search.close()
