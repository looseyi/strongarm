from capstone.arm64 import *
from typing import *
from objc_instruction import *
from macho_binary import MachoBinary
from debug_util import DebugUtil


class ObjcFunctionAnalyzer(object):
    def __init__(self, binary, instructions):
        # type: (MachoBinary, List[CsInsn]) -> None
        try:
            self.start_address = instructions[0].address
            last_idx = len(instructions) - 1
            self.end_address = instructions[last_idx].address
        except IndexError as e:
            raise RuntimeError('ObjcFunctionAnalyzer was passed invalid instructions')

        self.binary = binary
        self.analyzer = MachoAnalyzer.get_analyzer(binary)
        self._instructions = instructions
        self.__call_targets = None

    def debug_print(self, idx, output):
        DebugUtil.log(self, 'func({} + {}) {}'.format(
            hex(int(self._instructions[0].address)),
            hex(idx),
            output
        ))

    @classmethod
    def get_function_analyzer(cls, binary, start_address):
        analyzer = MachoAnalyzer.get_analyzer(binary)
        instructions = analyzer.get_function_instructions(start_address)
        return ObjcFunctionAnalyzer(binary, instructions)

    @property
    def call_targets(self):
        if self.__call_targets is not None:
            return self.__call_targets
        targets = []

        last_branch_idx = 0
        while True:
            next_branch = self.next_branch(last_branch_idx)
            if not next_branch:
                # parsed every branch in this function
                break
            targets.append(next_branch)
            # record that we checked this branch
            last_branch_idx = self._instructions.index(next_branch.raw_instr)
            # add 1 to last branch so on the next loop iteration,
            # we start searching for branches following this instruction which is known to have a branch
            last_branch_idx += 1

        self.__call_targets = targets
        return targets

    def can_execute_call(self, call_address):
        self.debug_print(0, 'recursively searching for invocation of {}'.format(hex(int(call_address))))
        for target in self.call_targets:
            instr_idx = self._instructions.index(target.raw_instr)

            # is this a direct call?
            if target.destination_address == call_address:
                self.debug_print(instr_idx, 'found call to {} at {}'.format(
                    hex(int(call_address)),
                    hex(int(target.address))
                ))
                return True
            # don't try to follow this path if it's an external symbol and not an objc_msgSend call
            if target.is_external_c_call and not target.is_msgSend_call:
                self.debug_print(instr_idx, '{}(...)'.format(
                    target.symbol
                ))
                continue
            # don't try to follow path if it's an internal branch (i.e. control flow within this function)
            # any internal branching will eventually be covered by call_targets,
            # so there's no need to follow twice
            if self.is_local_branch(target):
                self.debug_print(instr_idx, 'local goto -> {}'.format(hex(int(target.destination_address))))
                continue

            # might be objc_msgSend to object of class defined outside binary
            if target.is_external_objc_call:
                self.debug_print(instr_idx, 'objc_msgSend(...) to external class, selref at {}'.format(
                    hex(int(target.selref))
                ))
                continue

            # in debug log, print whether this is a function call or objc_msgSend call
            call_convention = 'objc_msgSend(id, ' if target.is_msgSend_call else 'func('
            self.debug_print(instr_idx, '{}{})'.format(
                call_convention,
                hex(int(target.destination_address)),
            ))

            # recursively check if this destination can call target address
            child_analyzer = ObjcFunctionAnalyzer.get_function_analyzer(self.binary, target.destination_address)
            if child_analyzer.can_execute_call(call_address):
                self.debug_print(instr_idx, 'found call to {} in child code path'.format(
                    hex(int(call_address))
                ))
                return True
        # no code paths reach desired call
        self.debug_print(len(self._instructions), 'no code paths reach {}'.format(
            hex(int(call_address))
        ))
        return False

    @classmethod
    def format_instruction(cls, instr):
        # type: (CsInsn) -> Text
        """Stringify a CsInsn for printing
        :param instr: Instruction to create formatted string representation for
        :return: Formatted string representing instruction
        """
        return "{addr}:\t{mnemonic}\t{ops}".format(addr=hex(int(instr.address)),
                                                   mnemonic=instr.mnemonic,
                                                   ops=instr.op_str)

    def track_reg(self, reg):
        # type: (Text) -> List[Text]
        """
        Track the flow of data starting in a register through a list of instructions
        :param reg: Register containing initial location of data
        :return: List containing all registers which contain data originally in reg
        """
        # list containing all registers which hold the same value as initial argument reg
        regs_holding_value = [reg]
        for instr in self._instructions:
            # TODO(pt) track other versions of move w/ suffix e.g. movz
            # do instructions like movz only operate on literals? we only care about reg to reg
            if instr.mnemonic == 'mov':
                if len(instr.operands) != 2:
                    raise RuntimeError('Encountered mov with more than 2 operands! {}'.format(
                        self.format_instruction(instr)
                    ))
                # in mov instruction, operands[0] is dst and operands[1] is src
                src = instr.reg_name(instr.operands[1].value.reg)
                dst = instr.reg_name(instr.operands[0].value.reg)

                # check if we're copying tracked value to another register
                if src in regs_holding_value and dst not in regs_holding_value:
                    # add destination register to list of registers containing value to track
                    regs_holding_value.append(dst)
                # check if we're copying something new into a register previously containing tracked value
                elif dst in regs_holding_value and src not in regs_holding_value:
                    # register being overwrote -- no longer contains tracked value, so remove from list
                    regs_holding_value.remove(dst)
        return regs_holding_value

    def next_blr_to_reg(self, reg, start_index):
        # type: (Text, int) -> CsInsn
        """
        Search for the next 'blr' instruction to a target register, starting from the instruction at start_index
        :param reg: Register whose 'branch to' instruction should be found
        :param start_index: Instruction index to begin search at
        :return: Index of next 'blr' instruction to reg
        """
        index = start_index
        for instr in self._instructions[start_index::]:
            if instr.mnemonic == 'blr':
                dst = instr.operands[0]
                if instr.reg_name(dst.value.reg) == reg:
                    return instr
            index += 1
        return None

    def next_branch(self, start_index):
        branch_mnemonics = ['b',
                            'bl',
                            'bx',
                            'blx',
                            'bxj',
                            ]
        for idx, instr in enumerate(self._instructions[start_index::]):
            if instr.mnemonic in branch_mnemonics:
                # found next branch!
                # wrap in ObjcBranchInstr object
                branch_instr = ObjcBranchInstr(self.binary, instr)

                # if this is an objc_msgSend target, patch destination_address to be the address of the targeted IMP
                # note! this means destination_address is *not* the actual destination address of the instruction
                # the *real* destination will be a stub function corresponding to __objc_msgSend, but
                # knowledge of this is largely useless, and the much more valuable piece of information is
                # which function the selector passed to objc_msgSend corresponds to.
                # therefore, replace the 'real' destination address with the requested IMP
                if branch_instr.is_msgSend_call:
                    selref = None
                    # attempt to get an IMP for this selref
                    try:
                        selref = self.get_selref(branch_instr.raw_instr)
                        sel_imp = self.analyzer.imp_for_selref(selref)
                    except RuntimeError as e:
                        instr_idx = start_index + idx
                        self.debug_print(instr_idx, 'bl <objc_msgSend> target cannot be determined statically')
                        sel_imp = None

                    # if we couldn't find an IMP for this selref,
                    # it is defined in a class outside this binary
                    if not sel_imp:
                        branch_instr.is_external_objc_call = True

                    branch_instr.selref = selref
                    branch_instr.destination_address = sel_imp

                return branch_instr
        return None

    def is_local_branch(self, branch_instruction):
        return self.start_address <= branch_instruction.destination_address <= self.end_address

    def get_selref(self, msgsend_instr):
        # type: (CsInsn) -> int
        """Retrieve contents of x1 register when control is at provided instruction

        Args:
              msgsend_instr: Instruction at which data in x1 should be found

        Returns:
              Data stored in x1 at execution of msgsend_instr

        """
        msgsend_index = self._instructions.index(msgsend_instr)
        # retrieve whatever data is in x1 at the index of this msgSend call
        return self.determine_register_contents('x1', msgsend_index)

    def _trimmed_reg_name(self, reg_name):
        # type: (Text) -> Text
        """Remove 'x' or 'w' from general purpose register name
        This is so the register strings 'x22' and 'w22', which are two slices of the same register,
        map to the same register.

        Will return non-GP registers, such as 'sp', as-is.

        Args:
              reg_name: Full register name to trim

        Returns:
              Register name with trimmed size prefix, or unmodified name if not a GP register

        """
        if reg_name[0] in ['x', 'w']:
            return reg_name[1::]
        return reg_name

    def determine_register_contents(self, desired_reg, start_index):
        # type: (Text, int) -> int
        """Analyze instructions backwards from start_index to find data in reg
        This function will read all instructions until it gathers all data and assignments necessary to determine
        value of desired_reg.

        For example, if we have a function like the following:
        15  | adrp x8, #0x1011bc000
        16  | ldr x22, [x8, #0x378]
        ... | ...
        130 | mov x1, x22
        131 | bl objc_msgSend <-- ObjcDataFlowAnalyzer.find_reg_value(31, 'x1') = 0x1011bc378

        Args:
            desired_reg: string containing name of register whose data should be determined
            start_index: the instruction index at which desired_reg's value should be found

        Returns:
              An int representing the contents of the register

        """
        target_addr = self._instructions[0].address + (start_index * 4)
        DebugUtil.log(self, 'analyzing data flow to determine data in {} at {}'.format(
            desired_reg,
            hex(int(target_addr))
        ))

        # TODO(PT): write CsInsn instructions by hand to make this function easy to test w/ different scenarios
        # List of registers whose values we need to find
        # initially, we need to find the value of whatever the user requested
        unknown_regs = [self._trimmed_reg_name(desired_reg)]
        # map of name -> value for registers whose values have been resolved to an immediate
        determined_values = {}
        # map of name -> (name, value). key is register needing to be resolved,
        # value is tuple containing (register containing source value, signed offset from source register)
        needed_links = {}

        # find data starting backwards from start_index
        for instr in self._instructions[start_index::-1]:
            # still looking for anything?
            if len(unknown_regs) == 0:
                # found everything we need
                break

            # we only care about instructions that could be moving data between registers
            if len(instr.operands) < 2:
                continue
            # some instructions will have the same format as register transformations,
            # but are actually unrelated to what we're looking for
            # for example, str x1, [sp, #0x38] would be identified by this function as moving something from sp into
            # x1, but with that particular instruction it's the other way around: x1 is being stored somewhere offset
            # from sp.
            # to avoid this bug, we need to exclude some instructions from being looked at by this method.
            excluded_instructions = [
                'str',
            ]
            if instr.mnemonic in excluded_instructions:
                continue

            dst = instr.operands[0]
            src = instr.operands[1]

            # we're only interested in instructions whose destination is a register
            if dst.type != ARM64_OP_REG:
                continue
            dst_reg_name = self._trimmed_reg_name(instr.reg_name(dst.value.reg))
            # is this register needed for us to determine the value of the requested register?
            if dst_reg_name not in unknown_regs:
                continue

            # src might not actually be the first operand
            # this could be an instruction like 'orr', whose invocation might look like this:
            # orr x1, wzr, #0x2
            # here, wzr is used as a 'trick' and the real source is the third operand
            # try to detect this pattern
            # zr indicates zero-register
            if len(instr.operands) > 2:
                if src.type == ARM64_OP_REG:
                    if self._trimmed_reg_name(instr.reg_name(src.value.reg)) == 'zr':
                        src = instr.operands[2]

            if src.type == ARM64_OP_IMM:
                # we now know the immediate value in dst_reg_name
                # remove it from unknown list
                unknown_regs.remove(dst_reg_name)
                # add it to known list, along with its value
                determined_values[dst_reg_name] = src.value.imm
            elif src.type == ARM64_OP_REG:
                # we now need the value of src before dst can be determined
                # move dst from list of unknown registers to list of registers waiting for another value
                unknown_regs.remove(dst_reg_name)
                src_reg_name = self._trimmed_reg_name(instr.reg_name(src.value.reg))

                # do we already know the exact value of the source?
                if src_reg_name in determined_values:
                    # value of dst will just be whatever src contains
                    dst_value = determined_values[src_reg_name]
                    determined_values[dst_reg_name] = dst_value
                else:
                    # we'll need to resolve src before we can know dst,
                    # add dst -> src to links list
                    needed_links[dst_reg_name] = src_reg_name, 0
                    # and add src to registers to search for
                    unknown_regs.append(src_reg_name)
            elif src.type == ARM64_OP_MEM:
                src_reg_name = self._trimmed_reg_name(instr.reg_name(src.mem.base))
                # dst is being assigned to the value of another register, plus a signed offset
                unknown_regs.remove(dst_reg_name)
                if src_reg_name in determined_values:
                    # we know dst value is value in src plus an offset,
                    # and we know what's in source
                    # we now know the value of dst
                    dst_value = determined_values[src_reg_name] + src.mem.disp
                    determined_values[dst_reg_name] = dst_value
                else:
                    # we must find src's value to resolve dst
                    unknown_regs.append(src_reg_name)
                    # add dst -> src + offset to links list
                    needed_links[dst_reg_name] = src_reg_name, src.mem.disp

        # if any of the data dependencies for our desired register uses the stack pointer,
        # there's no way we can resolve the value.
        stack_pointer_reg = 'sp'
        if stack_pointer_reg in needed_links:
            raise RuntimeError('{} contents depends on stack, cannot determine statically'.format(desired_reg))

        # once we've broken out of the above loop, we should have all the values we need to compute the
        # final value of the desired register.
        # additionally, it should be guaranteed that the unknown values list is empty
        if len(unknown_regs):
            DebugUtil.log(self, 'Exited loop with unknown list! instr 0 {} idx {} unknown {} links {} known {}'.format(
                hex(int(self._instructions[0].address)),
                start_index,
                unknown_regs,
                needed_links,
                determined_values,
            ))
            raise RuntimeError('Data-flow loop exited before all unknowns were marked')

        # for every register in the waiting list,
        # cross reference all its dependent variables to calculate the final value
        return self._resolve_register_value_from_data_links(desired_reg, needed_links, determined_values)

    def _resolve_register_value_from_data_links(self, desired_reg, links, resolved_registers):
        # type: (Text, Dict[Text, Tuple[Text, int]], Dict[Text, int]) -> int
        """Resolve data dependencies for each register to find final value of desired_reg
        This method will throw an Exception if the arguments cannot be resolved.

        Args:
              desired_reg: string containing name of register whose value should be determined
              links: mapping of register data dependencies. For example, x1's value might be x22's value plus an
              offset of 0x300, so links['x1'] = ('x22', 0x300)
              resolved_registers: mapping of registers whose final value is already known

        Returns:
            The final value contained in desired_reg after resolving all data dependencies
        """

        if len(resolved_registers) == 0:
            raise RuntimeError('need at least one known value to resolve data dependencies')

        desired_reg = self._trimmed_reg_name(desired_reg)
        if desired_reg not in links and desired_reg not in resolved_registers:
            raise RuntimeError('invalid data set? desired_reg {} can\'t be determined from '
                               'links {}, resolved_registers {}'.format(
                desired_reg,
                links,
                resolved_registers,
            ))

        # do we know the value of this register?
        if desired_reg in resolved_registers:
            DebugUtil.log(self, 'x{} is a known immediate: {}'.format(
                desired_reg,
                hex(int(resolved_registers[desired_reg]))
            ))
            return resolved_registers[desired_reg]

        # to determine value in desired_reg,
        # we must find the value of source_reg, and then apply any offset
        source_reg, offset = links[desired_reg]
        DebugUtil.log(self, 'x{} has data dependency: [x{}, #{}]'.format(
            desired_reg,
            source_reg,
            hex(int(offset))
        ))

        # resolve source reg value, then add offset
        final_val = self._resolve_register_value_from_data_links(source_reg, links, resolved_registers) + offset

        # this link has been resolved! remove from links list
        links.pop(desired_reg)
        # add to list of known values
        resolved_registers[desired_reg] = final_val

        DebugUtil.log(self, 'x{} resolved to {}'.format(
            desired_reg,
            hex(int(final_val))
        ))
        return final_val


class ObjcBlockAnalyzer(ObjcFunctionAnalyzer):
    def __init__(self, binary, instructions, initial_block_reg):
        ObjcFunctionAnalyzer.__init__(self, binary, instructions)

        self.registers_containing_block = self.track_reg(initial_block_reg)
        self.load_reg, self.load_index = self.find_block_load()
        self.invoke_instr = self.find_block_invoke()
        self.invoke_idx = self._instructions.index(self.invoke_instr)

    def find_block_load(self):
        """
        Find instruction where Block->invoke is loaded into
        :return: Tuple of register containing Block->invoke, and the index this instruction was found at
        """
        index = 0
        for instr in self._instructions:
            if instr.mnemonic == 'ldr':
                if len(instr.operands) != 2:
                    raise RuntimeError('Encountered ldr with more than 2 operands! {}'.format(
                        self.format_instruction(instr)
                    ))
                # we're looking for an instruction in the format:
                # ldr <reg> [<reg_containing_block>, #0x10]
                # block->invoke is always 0x10 from start of block
                # so if we see the above then we know we're loading the block's executable start addr
                dst = instr.operands[0]
                src = instr.operands[1]
                if src.type == ARM64_OP_MEM:
                    if instr.reg_name(src.mem.base) in self.registers_containing_block and src.mem.disp == 0x10:
                        # found load of block's invoke addr!
                        return instr.reg_name(dst.value.reg), index
            index += 1

    def find_block_invoke(self):
        # type: () -> CsInsn
        return self.next_blr_to_reg(self.load_reg, self.load_index)

    def get_block_arg(self, arg_index):
        # type: (int) -> int
        """
        Starting from the index where a function is called, search backwards for the assigning of
        a register corresponding to function argument at index arg_index
        Currently this function will only detect arguments who are assigned to an immediate value
        :param arg_index: Positional argument index (0 for first argument, 1 for second, etc.)
        :return: Index of instruction where argument is assigned
        """
        desired_register = u'x{}'.format(arg_index)

        invoke_index = self._instructions.index(self.invoke_instr)
        for instr in self._instructions[invoke_index::-1]:
            if instr.mnemonic == 'movz' or instr.mnemonic == 'mov':
                # arg1 will be stored in x1
                dst = instr.operands[0]
                src = instr.operands[1]
                if instr.reg_name(dst.value.reg) == desired_register:
                    # return immediate value if source is not a register
                    if instr.mnemonic == 'movz':
                        return src.value.imm
                    # source is a register, return register name
                    return instr.reg_name(src.value.reg)
