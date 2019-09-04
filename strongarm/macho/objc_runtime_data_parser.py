import logging
from ctypes import sizeof, c_uint32
from typing import List, Optional, Dict

from strongarm.macho.macho_definitions import VirtualMemoryPointer
from strongarm.macho.arch_independent_structs import (
    ObjcClassRawStruct,
    ObjcDataRawStruct,
    ObjcMethodStruct,
    ObjcMethodListStruct,
    ObjcIvarStruct,
    ObjcIvarListStruct,
    ObjcCategoryRawStruct,
    ObjcProtocolRawStruct,
    ObjcProtocolListStruct,
    ArchIndependentStructure
)
from strongarm.debug_util import DebugUtil
from strongarm.macho.macho_binary import MachoBinary


class ObjcClass:
    __slots__ = ['raw_struct', 'name', 'selectors', 'ivars', 'protocols']

    def __init__(self,
                 raw_struct: ArchIndependentStructure,
                 name: str,
                 selectors: List['ObjcSelector'],
                 ivars: List['ObjcIvar'] = None,
                 protocols: List['ObjcProtocol'] = None) -> None:
        self.name = name
        self.selectors = selectors
        self.raw_struct = raw_struct
        self.ivars = ivars if ivars else []
        self.protocols = protocols if protocols else []


class ObjcCategory(ObjcClass):
    __slots__ = ['raw_struct', 'name', 'base_class', 'category_name', 'selectors', 'ivars', 'protocols']

    def __init__(self,
                 raw_struct: ObjcCategoryRawStruct,
                 base_class: str,
                 category_name: str,
                 selectors: List['ObjcSelector'],
                 ivars: List['ObjcIvar'] = None,
                 protocols: List['ObjcProtocol'] = None) -> None:
        self.base_class = base_class
        self.category_name = category_name

        # ObjcCategory.name includes the base class + the cat-name
        # That way, callers don't need to check the ObjcClass instance type to get the 'right' value
        full_name = f'{base_class} ({category_name})'
        super().__init__(raw_struct, full_name, selectors, ivars, protocols)


class ObjcProtocol(ObjcClass):
    pass


class ObjcSelector:
    __slots__ = ['name', 'selref', 'implementation', 'is_external_definition']

    def __init__(self, name: str, selref: Optional['ObjcSelref'], implementation: Optional[VirtualMemoryPointer]) -> None:
        self.name = name
        self.selref = selref
        self.implementation = implementation

        self.is_external_definition = (not self.implementation)

    def __str__(self) -> str:
        imp_addr = 'NaN'
        if self.implementation:
            imp_addr = hex(int(self.implementation))
        return f'<@selector({self.name}) at {imp_addr}>'

    __repr__ = __str__


class ObjcIvar:
    __slots__ = ['name', 'class_name', 'field_offset']

    def __init__(self, name: str, class_name: str, offset: int):
        self.name = name
        self.class_name = class_name
        self.field_offset = offset

    def __str__(self) -> str:
        return f'<@ivar {self.class_name}* {self.name}, off @ {self.field_offset}>'

    __repr__ = __str__


class ObjcSelref:
    __slots__ = ['source_address', 'destination_address', 'selector_literal']

    def __init__(self,
                 source_address: VirtualMemoryPointer,
                 destination_address: VirtualMemoryPointer,
                 selector_literal: str) -> None:
        self.source_address = source_address
        self.destination_address = destination_address
        self.selector_literal = selector_literal


class ObjcRuntimeDataParser:
    def __init__(self, binary: MachoBinary) -> None:
        self.binary = binary
        DebugUtil.log(self, 'Parsing ObjC runtime info... (this may take a while)')

        DebugUtil.log(self, 'Step 1: Parsing selrefs...')
        self._selref_ptr_to_selector_map: Dict[VirtualMemoryPointer, ObjcSelector] = {}
        self._selector_literal_ptr_to_selref_map: Dict[VirtualMemoryPointer, ObjcSelref] = {}
        # this method populates self._selector_literal_ptr_to_selref_map and self._selref_ptr_to_selector_map
        self._parse_selrefs()

        DebugUtil.log(self, 'Step 2: Parsing classes, categories, and protocols...')
        self.classes = self._parse_class_and_category_info()
        self.protocols = self._parse_global_protocol_info()

        DebugUtil.log(self, 'Step 3: Resolving symbol name to source dylib map...')
        self._sym_to_dylib_path = self._parse_linked_dylib_symbols()

    def _parse_linked_dylib_symbols(self) -> Dict[str, str]:
        syms_to_dylib_path = {}

        symtab = self.binary.symtab
        symtab_contents = self.binary.symtab_contents
        dysymtab = self.binary.dysymtab
        for undef_sym_idx in range(dysymtab.nundefsym):
            symtab_idx = dysymtab.iundefsym + undef_sym_idx
            sym = symtab_contents[symtab_idx]

            strtab_idx = sym.n_un.n_strx
            string_file_address = symtab.stroff + strtab_idx
            symbol_name = self.binary.get_full_string_from_start_address(string_file_address, virtual=False)
            if not symbol_name:
                logging.error(f'Could not get symbol name at address {string_file_address}')
                continue

            library_ordinal = self._library_ordinal_from_n_desc(sym.n_desc)
            source_name = self.binary.dylib_name_for_library_ordinal(library_ordinal)

            syms_to_dylib_path[symbol_name] = source_name
        return syms_to_dylib_path

    def path_for_external_symbol(self, symbol: str) -> Optional[str]:
        if symbol in self._sym_to_dylib_path:
            return self._sym_to_dylib_path[symbol]
        return None

    @staticmethod
    def _library_ordinal_from_n_desc(n_desc: int) -> int:
        return (n_desc >> 8) & 0xff

    def _parse_selrefs(self) -> None:
        """Parse the binary's selref list, and store the data.

        This method populates self._selector_literal_ptr_to_selref_map.
        It also *PARTLY* populates self._selref_ptr_to_selector_map. All selrefs keys will have an ObjcSelector
        value, but none of the ObjcSelector objects will have their `implementation` field filled, because
        at this point in the parse we do not yet know the implementations of each selector. ObjcSelectors which we
        later find an implementation for are updated in self.read_selectors_from_methlist_ptr"""
        selref_pointers, selector_literal_pointers = self.binary.read_pointer_section('__objc_selrefs')
        # sanity check
        if len(selref_pointers) != len(selector_literal_pointers):
            raise RuntimeError('read invalid data from __objc_selrefs')

        for selref_ptr, selector_literal_ptr in zip(selref_pointers, selector_literal_pointers):
            # read selector string literal from selref pointer
            selector_string = self.binary.get_full_string_from_start_address(selector_literal_ptr)
            if not selector_string:
                continue  # but all selectors should have a name
            wrapped_selref = ObjcSelref(selref_ptr, selector_literal_ptr, selector_string)

            # map the selector string pointer to the ObjcSelref
            self._selector_literal_ptr_to_selref_map[selector_literal_ptr] = wrapped_selref
            # add second mapping in selref list
            # we don't know the implementation address yet but it will be updated when we parse method lists
            self._selref_ptr_to_selector_map[selref_ptr] = ObjcSelector(selector_string, wrapped_selref, None)

    def selector_for_selref(self, selref_addr: VirtualMemoryPointer) -> Optional[ObjcSelector]:
        if selref_addr in self._selref_ptr_to_selector_map:
            return self._selref_ptr_to_selector_map[selref_addr]

        # selref wasn't referenced in classes implemented within the binary
        # make sure it's a valid selref
        selref = [x for x in self._selector_literal_ptr_to_selref_map.values() if x.source_address == selref_addr]
        if not len(selref):
            return None
        _selref = selref[0]

        # therefore, the _selref must refer to a selector which is defined outside this binary
        # this is fine, just construct an ObjcSelector with what we know
        sel = ObjcSelector(_selref.selector_literal, _selref, None)
        return sel

    def selrefs_to_selectors(self) -> Dict[VirtualMemoryPointer, ObjcSelector]:
        return self._selref_ptr_to_selector_map

    def selref_for_selector_name(self, selector_name: str) -> Optional[VirtualMemoryPointer]:
        selref_list = [x for x in self._selref_ptr_to_selector_map
                       if self._selref_ptr_to_selector_map[x].name == selector_name]
        if len(selref_list):
            return selref_list[0]
        return None

    def get_method_imp_addresses(self, selector: str) -> List[VirtualMemoryPointer]:
        """Given a selector, return a list of virtual addresses corresponding to the start of each IMP for that SEL
        """
        imp_addresses = []
        for objc_class in self.classes:
            for objc_sel in objc_class.selectors:
                if objc_sel.name == selector:
                    if objc_sel.implementation:
                        imp_addresses.append(objc_sel.implementation)
        return imp_addresses

    def _parse_objc_classes(self) -> List[ObjcClass]:
        """Read Objective-C class data in __objc_classlist, __objc_data to get classes and selectors in binary
        """
        DebugUtil.log(self, 'Cross-referencing __objc_classlist, __objc_class, and __objc_data entries...')
        parsed_objc_classes = []
        classlist_pointers = self._get_classlist_pointers()
        for ptr in classlist_pointers:
            objc_class = self._get_objc_class_from_classlist_pointer(ptr)
            if objc_class:
                parsed_class = None
                # parse the instance method list
                objc_data_struct = self._get_objc_data_from_objc_class(objc_class)
                if objc_data_struct:
                    # the class's associated struct __objc_data contains the method list
                    parsed_class = self._parse_objc_data_entry(objc_class, objc_data_struct)

                # parse the metaclass if it exists
                # the class stores instance methods and the metaclass's method list contains class methods
                # the metaclass has the same name as the actual class
                metaclass = self._get_objc_class_from_classlist_pointer(objc_class.metaclass)
                if metaclass:
                    objc_data_struct = self._get_objc_data_from_objc_class(metaclass)
                    if objc_data_struct:
                        parsed_metaclass = self._parse_objc_data_entry(objc_class, objc_data_struct)
                        if parsed_class:
                            # add in selectors from the metaclass to the real class
                            parsed_class.selectors += parsed_metaclass.selectors
                        else:
                            # no base class found, set the base class to the metaclass
                            parsed_class = parsed_metaclass

                # sanity check
                # ensure we either found a class or metaclass
                if not parsed_class:
                    raise RuntimeError(f'Failed to parse classref {hex(ptr)}')
                parsed_objc_classes.append(parsed_class)

        return parsed_objc_classes

    def _parse_objc_categories(self) -> List[ObjcCategory]:
        DebugUtil.log(self, 'Cross referencing __objc_catlist, __objc_category, and __objc_data entries...')
        parsed_categories = []
        category_pointers = self._get_catlist_pointers()
        for ptr in category_pointers:
            objc_category_struct = self._get_objc_category_from_catlist_pointer(ptr)
            if objc_category_struct:
                parsed_category = self._parse_objc_category_entry(objc_category_struct)
                parsed_categories.append(parsed_category)
        return parsed_categories

    def _parse_class_and_category_info(self) -> List[ObjcClass]:
        """Parse classes and categories referenced by __objc_classlist and __objc_catlist
        """
        classes: List[ObjcClass] = []
        classes += self._parse_objc_classes()
        classes += self._parse_objc_categories()
        return classes

    def _parse_global_protocol_info(self) -> List[ObjcProtocol]:
        """Parse protocols which code in the app conforms to, referenced by __objc_protolist
        """
        DebugUtil.log(self, 'Cross referencing __objc_protolist, __objc_protocol, and __objc_data entries...')
        protocol_pointers = self._get_protolist_pointers()
        return self._parse_protocol_ptr_list(protocol_pointers)

    def read_ivars_from_ivarlist_ptr(self, ivarlist_ptr: VirtualMemoryPointer) -> List[ObjcIvar]:
        """Given the virtual address of an ivar list, return a List of each encoded ObjcIvar
        """
        ivarlist = self.binary.read_struct(ivarlist_ptr, ObjcIvarListStruct, virtual=True)
        ivars: List[ObjcIvar] = []
        # Parse each ivar struct which follows the ivarlist
        ivar_struct_ptr = ivarlist_ptr + ivarlist.sizeof
        for _ in range(ivarlist.count):
            ivar_struct = self.binary.read_struct(ivar_struct_ptr, ObjcIvarStruct, virtual=True)

            ivar_name = self.binary.get_full_string_from_start_address(ivar_struct.name)
            class_name = self.binary.get_full_string_from_start_address(ivar_struct.type)
            field_offset = self.binary.read_word(ivar_struct.offset_ptr, word_type=c_uint32)

            # class_name and field_offset can be falsey ('' and 0), so don't include them in this sanity check
            if not ivar_name:
                raise ValueError(f'Failed to read ivar data for ivar entry @ {hex(ivar_struct_ptr)}')

            ivar = ObjcIvar(ivar_name, class_name, field_offset) # type: ignore
            ivars.append(ivar)

            ivar_struct_ptr += ivar_struct.sizeof
        return ivars

    def read_selectors_from_methlist_ptr(self, methlist_ptr: VirtualMemoryPointer) -> List[ObjcSelector]:
        """Given the virtual address of a method list, return a List of ObjcSelectors encapsulating each method
        """
        methlist = self.binary.read_struct(methlist_ptr, ObjcMethodListStruct, virtual=True)
        selectors: List[ObjcSelector] = []
        # parse every entry in method list
        # the first entry appears directly after the ObjcMethodListStruct
        method_entry_off = methlist_ptr + methlist.sizeof
        for i in range(methlist.methcount):
            method_ent = self.binary.read_struct(method_entry_off, ObjcMethodStruct, virtual=True)
            # byte-align IMP
            method_ent.implementation &= ~0x3

            symbol_name = self.binary.get_full_string_from_start_address(method_ent.name)
            if not symbol_name:
                raise ValueError(f'Could not get symbol name for {method_ent.name}')
            # attempt to find corresponding selref
            selref = self._selector_literal_ptr_to_selref_map.get(method_ent.name)

            selector = ObjcSelector(symbol_name, selref, method_ent.implementation)
            selectors.append(selector)

            # save this selector in the selref pointer -> selector map
            if selref:
                # if this selector is already in the map, check if we now know the implementation addr
                # we could have parsed the selector literal/selref pair in _parse_selrefs() but not have known the
                # implementation, but do now. It's also possible the selref is an external method, and thus will not
                # have a local implementation.
                if selref.source_address in self._selref_ptr_to_selector_map:
                    previously_parsed_selector = self._selref_ptr_to_selector_map[selref.source_address]
                    if not previously_parsed_selector.implementation:
                        # delete the old entry, and add back in the next line
                        del self._selref_ptr_to_selector_map[selref.source_address]
                self._selref_ptr_to_selector_map[selref.source_address] = selector

            method_entry_off += method_ent.sizeof
        return selectors

    def _parse_objc_protocol_entry(self, objc_protocol_struct: ObjcProtocolRawStruct) -> ObjcProtocol:
        symbol_name = self.binary.get_full_string_from_start_address(objc_protocol_struct.name)
        if not symbol_name:
            raise ValueError(f"Could not get symbol name for {objc_protocol_struct.name}")

        selectors: List[ObjcSelector] = []
        if objc_protocol_struct.required_instance_methods:
            selectors += self.read_selectors_from_methlist_ptr(objc_protocol_struct.required_instance_methods)
        if objc_protocol_struct.required_class_methods:
            selectors += self.read_selectors_from_methlist_ptr(objc_protocol_struct.required_class_methods)
        if objc_protocol_struct.optional_instance_methods:
            selectors += self.read_selectors_from_methlist_ptr(objc_protocol_struct.optional_instance_methods)
        if objc_protocol_struct.optional_class_methods:
            selectors += self.read_selectors_from_methlist_ptr(objc_protocol_struct.optional_class_methods)

        return ObjcProtocol(objc_protocol_struct, symbol_name, selectors)

    def _parse_objc_category_entry(self, objc_category_struct: ObjcCategoryRawStruct) -> ObjcCategory:
        symbol_name = self.binary.get_full_string_from_start_address(objc_category_struct.name)
        if not symbol_name:
            raise ValueError(f"Could not get symbol name for {objc_category_struct.name}")

        selectors: List[ObjcSelector] = []
        protocols: List[ObjcProtocol] = []
        # TODO(PT): if we want to parse the name of the base class, grab the destination pointer from entries in
        # __objc_classrefs; this will be the same as the address in .base_class, and by cross-reffing we can get the
        # name of the class symbol (like _OBJC_CLASS_$_NSURLRequest)
        base_class = '$_Unknown_Class'

        # if the class implements no methods, the pointer to method list will be the null pointer
        # TODO(PT): we could add some flag to keep track of whether a given sel is an instance or class method
        if objc_category_struct.instance_methods:
            selectors += self.read_selectors_from_methlist_ptr(objc_category_struct.instance_methods)
        if objc_category_struct.class_methods:
            selectors += self.read_selectors_from_methlist_ptr(objc_category_struct.class_methods)
        if objc_category_struct.base_protocols:
            # TODO(PT): perhaps these should be combined into one call
            protocol_pointers = self._protolist_ptr_to_protocol_ptr_list(objc_category_struct.base_protocols)
            protocols += self._parse_protocol_ptr_list(protocol_pointers)

        return ObjcCategory(objc_category_struct, base_class, symbol_name, selectors, protocols=protocols)

    def _parse_objc_data_entry(self,
                               objc_class_struct: ObjcClassRawStruct,
                               objc_data_struct: ObjcDataRawStruct) -> ObjcClass:
        symbol_name = self.binary.get_full_string_from_start_address(objc_data_struct.name)
        if not symbol_name:
            raise ValueError(f"Could not get symbol name for {objc_data_struct.name}")

        selectors: List[ObjcSelector] = []
        protocols: List[ObjcProtocol] = []
        ivars: List[ObjcIvar] = []
        # if the class implements no methods, base_methods will be the null pointer
        if objc_data_struct.base_methods:
            selectors += self.read_selectors_from_methlist_ptr(objc_data_struct.base_methods)
        # if the class doesn't conform to any protocols, base_protocols will be null
        if objc_data_struct.base_protocols:
            protocol_pointer_list = self._protolist_ptr_to_protocol_ptr_list(objc_data_struct.base_protocols)
            protocols += self._parse_protocol_ptr_list(protocol_pointer_list)
        # Parse ivar list
        if objc_data_struct.ivars:
            ivars += self.read_ivars_from_ivarlist_ptr(objc_data_struct.ivars)

        return ObjcClass(objc_class_struct, symbol_name, selectors, ivars, protocols)

    def _protolist_ptr_to_protocol_ptr_list(self, protolist_ptr: VirtualMemoryPointer) -> List[VirtualMemoryPointer]:
        """Accepts the virtual address of an ObjcProtocolListStruct, and returns List of protocol pointers it refers to.
        """
        protolist = self.binary.read_struct(protolist_ptr, ObjcProtocolListStruct, virtual=True)
        protocol_pointers: List[VirtualMemoryPointer] = []
        # pointers start directly after the 'count' field
        addr = protolist.binary_offset + protolist.sizeof
        for i in range(protolist.count):
            pointer = self.binary.read_word(addr)
            protocol_pointers.append(VirtualMemoryPointer(pointer))
            # step to next protocol pointer in list
            addr += sizeof(self.binary.platform_word_type)
        return protocol_pointers

    def _parse_protocol_ptr_list(self, protocol_ptrs: List[VirtualMemoryPointer]) -> List[ObjcProtocol]:
        protocols = []
        for protocol_ptr in protocol_ptrs:
            objc_protocol_struct = self._get_objc_protocol_from_pointer(protocol_ptr)
            if objc_protocol_struct:
                parsed_protocol = self._parse_objc_protocol_entry(objc_protocol_struct)
                protocols.append(parsed_protocol)
        return protocols

    def _get_catlist_pointers(self) -> List[VirtualMemoryPointer]:
        """Read pointers in __objc_catlist into list
        """
        _, catlist_pointers = self.binary.read_pointer_section('__objc_catlist')
        return catlist_pointers

    def _get_protolist_pointers(self) -> List[VirtualMemoryPointer]:
        """Read pointers in __objc_protolist into list
        """
        _, protolist_pointers = self.binary.read_pointer_section('__objc_protolist')
        return protolist_pointers

    def _get_classlist_pointers(self) -> List[VirtualMemoryPointer]:
        """Read pointers in __objc_classlist into list
        """
        _, classlist_pointers = self.binary.read_pointer_section('__objc_classlist')
        return classlist_pointers

    def _get_objc_category_from_catlist_pointer(
            self,
            category_struct_pointer: VirtualMemoryPointer) -> ObjcCategoryRawStruct:
        """Read a struct __objc_category from the location indicated by the provided __objc_catlist pointer
        """
        category_entry = self.binary.read_struct(category_struct_pointer, ObjcCategoryRawStruct, virtual=True)
        return category_entry

    def _get_objc_protocol_from_pointer(self, protocol_struct_pointer: VirtualMemoryPointer) -> ObjcProtocolRawStruct:
        """Read a struct __objc_protocol from the location indicated by the provided struct objc_protocol_list pointer
        """
        protocol_entry = self.binary.read_struct(protocol_struct_pointer, ObjcProtocolRawStruct, virtual=True)
        return protocol_entry

    def _get_objc_class_from_classlist_pointer(self, class_struct_pointer: VirtualMemoryPointer) -> ObjcClassRawStruct:
        """Read a struct __objc_class from the location indicated by the __objc_classlist pointer
        """
        class_entry = self.binary.read_struct(class_struct_pointer, ObjcClassRawStruct, virtual=True)

        # sanitize class_entry
        # the least significant 2 bits are used for flags
        # flag 0x1 indicates a Swift class
        # mod data pointer to ignore flags!
        class_entry.data &= ~0x3
        return class_entry

    def _get_objc_data_from_objc_class(self, objc_class: ObjcClassRawStruct) -> Optional[ObjcDataRawStruct]:
        """Read a struct __objc_data from a provided struct __objc_class
        If the struct __objc_class describe invalid or no corresponding data, None will be returned.
        """
        data_entry = self.binary.read_struct(objc_class.data, ObjcDataRawStruct, virtual=True)
        # ensure this is a valid entry
        if data_entry.name < self.binary.get_virtual_base():
            # TODO(PT): sometimes we'll get addresses passed to this method that are actually struct __objc_method
            # entries, rather than struct __objc_data entries. Investigate why this is.
            # This was observed on a 32bit binary, Esquire2
            DebugUtil.log(self, f'caught ObjcDataRaw struct with invalid fields at {hex(int(objc_class.data))}.'
                                f' data->name = {hex(data_entry.name)}')
            return None
        return data_entry
