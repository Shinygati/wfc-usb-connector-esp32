import pefile,capstone,sys
f,out=sys.argv[1],sys.argv[2]
pe=pefile.PE(f); base=pe.OPTIONAL_HEADER.ImageBase
md=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_32)
with open(out,'w') as o:
  for s in pe.sections:
    if not s.Characteristics & 0x20000000: continue
    code=s.get_data(); va=base+s.VirtualAddress
    off=0
    while off<len(code):
      n=0
      for i in md.disasm(code[off:],va+off):
        o.write('%08x: %-20s %s %s\n'%(i.address,i.bytes.hex(),i.mnemonic,i.op_str)); n+=i.size
      off+=n
      if off<len(code): o.write('%08x: %s db\n'%(va+off,code[off:off+1].hex())); off+=1
