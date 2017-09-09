from macho_definitions import *


class MachoParser(object):
    MH_MAGIC = 0xfeedface
    MH_CIGAM = 0xcefaedfe
    MH_MAGIC_64 = 0xfeedfacf
    MH_CIGAM_64 = 0xcffaedfe

    MH_CPU_ARCH_ABI64 = 0x01000000
    MH_CPU_TYPE_ARM = 12
    MH_CPU_TYPE_ARM64 = MH_CPU_TYPE_ARM | MH_CPU_ARCH_ABI64

    def __init__(self, filename):
        self.is_64bit = False
        self.is_swapped = False
        self.load_commands_offset = 0
        self._num_commands = 0
        self.magic = 0
        self._file = None
        self.cpu_type = CPU_TYPE.UNKNOWN

        self.header = None
        self.segments = {}
        self.sections = {}
        self.dysymtab = None
        self.symtab = None
        self.encryption_info = None

        self.parse(filename)

    def parse(self, filename):
        self._file = open(filename, 'rb')

        if not self.check_magic():
            print('Couldn\'t parse {}'.format(self._file.name))
            return
        self.is_64bit = self.magic_is_64()
        self.is_swapped = self.should_swap_bytes()
        self.parse_header()

    def check_magic(self):
        self._file.seek(0)
        self.magic = c_uint32.from_buffer(bytearray(self._file.read(sizeof(c_uint32)))).value
        if self.magic == self.MH_MAGIC or self.magic == self.MH_CIGAM:
            print('32-bit Mach-O binaries not yet supported.')
            return False
        elif self.magic == self.MH_MAGIC_64 or self.magic == self.MH_CIGAM_64:
            print('64-bit Mach-O magic ok')
            return True
        # unknown magic!
        print('Unrecognized file magic {}'.format(self.magic))
        return False

    def should_swap_bytes(self):
        return self.magic == self.MH_CIGAM_64 or self.magic == self.MH_CIGAM

    def magic_is_64(self):
        return self.magic == self.MH_MAGIC_64 or self.magic == self.MH_CIGAM_64

    def get_bytes(self, offset, size):
        self._file.seek(offset)
        return self._file.read(size)

    def parse_segments(self):
        pass

    def parse_header(self):
        header_bytes = self.get_bytes(0, sizeof(MachoHeader64))
        self.header = MachoHeader64.from_buffer(bytearray(header_bytes))

        if self.header.cputype == self.MH_CPU_TYPE_ARM:
            self.cpu_type = CPU_TYPE.ARMV7
        elif self.header.cputype == self.MH_CPU_TYPE_ARM64:
            self.cpu_type = CPU_TYPE.ARM64
        else:
            self.cpu_type = CPU_TYPE.UNKNOWN

        self._num_commands = self.header.ncmds
        self.load_commands_offset += sizeof(MachoHeader64)
        self.parse_segment_commands(self.load_commands_offset)

    def parse_segment_commands(self, offset):
        for i in range(self._num_commands):
            load_command_bytes = self.get_bytes(offset, sizeof(MachOLoadCommand))
            load_command = MachOLoadCommand.from_buffer(bytearray(load_command_bytes))
            # TODO(pt) handle byte swap of load_command
            if load_command.cmd == MachoLoadCommands.LC_SEGMENT:
                print('32-bit segments not supported!')
                continue

            if load_command.cmd == MachoLoadCommands.LC_SYMTAB:
                symtab_bytes = self.get_bytes(offset, sizeof(MachoSymtabCommand))
                self.symtab = MachoSymtabCommand.from_buffer(bytearray(symtab_bytes))
            elif load_command.cmd == MachoLoadCommands.LC_DYSYMTAB:
                dysymtab_bytes = self.get_bytes(offset, sizeof(MachoDysymtabCommand))
                self.dysymtab = MachoDysymtabCommand.from_buffer(bytearray(dysymtab_bytes))
            elif load_command.cmd == MachoLoadCommands.LC_ENCRYPTION_INFO_64:
                encryption_info_bytes = self.get_bytes(offset, sizeof(MachoEncryptionInfo64Command))
                self.encryption_info = MachoEncryptionInfo64Command.from_buffer(bytearray(encryption_info_bytes))
            elif load_command.cmd == MachoLoadCommands.LC_SEGMENT_64:
                segment_bytes = self.get_bytes(offset, sizeof(MachoSegmentCommand64))
                segment = MachoSegmentCommand64.from_buffer(bytearray(segment_bytes))
                # TODO(pt) handle byte swap of segment
                self.segments[segment.segname] = segment
                self.parse_sections(segment, offset)

            offset += load_command.cmdsize

    def parse_sections(self, segment, segment_offset):
        if not segment.nsects:
            return

        section_offset = segment_offset + sizeof(MachoSegmentCommand64)
        section_size = sizeof(MachoSection64)
        for i in range(segment.nsects):
            section_bytes = self.get_bytes(section_offset, sizeof(MachoSection64))
            section = MachoSection64.from_buffer(bytearray(section_bytes))
            # TODO(pt) handle byte swap of segment
            self.sections[section.sectname] = section

            section_offset += section_size

    def get_section_with_name(self, name):
        return self.sections[name]

    def get_section_content(self, section):
        return bytearray(self.get_bytes(section.offset, section.size))

    def get_virtual_base(self):
        text_seg = self.segments['__TEXT']
        return text_seg.vmaddr

    def __del__(self):
        self._file.close()