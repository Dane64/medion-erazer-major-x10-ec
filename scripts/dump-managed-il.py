#!/usr/bin/env python3

import argparse
import sys
from pathlib import Path

import dnfile
from dncil.cil.body.reader import read_method_body_from_bytes
from dncil.clr.token import StringToken, Token


TABLE_NAMES = {
    0x01: "TypeRef",
    0x02: "TypeDef",
    0x04: "Field",
    0x06: "MethodDef",
    0x0A: "MemberRef",
    0x11: "StandAloneSig",
    0x1B: "TypeSpec",
    0x2B: "MethodSpec",
}


class MetadataResolver:
    def __init__(self, pe):
        self.pe = pe
        self.method_owners = self._build_owner_map("MethodList")
        self.field_owners = self._build_owner_map("FieldList")

    def _build_owner_map(self, list_name):
        owners = {}
        for type_row in self.pe.net.mdtables.TypeDef.rows:
            for index in getattr(type_row, list_name):
                owners[id(index.row)] = type_row
        return owners

    @staticmethod
    def _type_name(row):
        namespace = str(getattr(row, "TypeNamespace", ""))
        name = str(getattr(row, "TypeName", ""))
        return f"{namespace}.{name}" if namespace else name

    def _row_name(self, table_name, row):
        if table_name in ("TypeDef", "TypeRef"):
            return self._type_name(row)
        if table_name == "MethodDef":
            owner = self.method_owners.get(id(row))
            prefix = f"{self._type_name(owner)}::" if owner else ""
            return prefix + str(row.Name)
        if table_name == "Field":
            owner = self.field_owners.get(id(row))
            prefix = f"{self._type_name(owner)}::" if owner else ""
            return prefix + str(row.Name)
        if table_name == "MemberRef":
            owner = getattr(getattr(row, "Class", None), "row", None)
            prefix = f"{self._row_name(type(owner).__name__.removesuffix('Row'), owner)}::" if owner else ""
            return prefix + str(row.Name)
        if table_name == "MethodSpec":
            method = getattr(getattr(row, "Method", None), "row", None)
            return self._row_name(type(method).__name__.removesuffix("Row"), method) if method else str(row)
        return str(getattr(row, "Name", row))

    def resolve(self, operand):
        if isinstance(operand, StringToken):
            try:
                return repr(str(self.pe.net.user_strings.get(operand.rid)))
            except Exception:
                return str(operand)
        if not isinstance(operand, Token):
            return str(operand)

        table_name = TABLE_NAMES.get(operand.table)
        table = getattr(self.pe.net.mdtables, table_name, None) if table_name else None
        if table is None or operand.rid == 0 or operand.rid > len(table.rows):
            return str(operand)
        return f"{table_name} {self._row_name(table_name, table.rows[operand.rid - 1])}"

    def method_name(self, row):
        return self._row_name("MethodDef", row)


def dump_method(pe, resolver, row):
    print(f"\n.method {resolver.method_name(row)} rva=0x{row.Rva:08x}")
    if not row.Rva:
        print("  <no managed body>")
        return

    body = read_method_body_from_bytes(pe.get_data(row.Rva))
    for instruction in body.instructions:
        operand = ""
        if instruction.operand is not None:
            if isinstance(instruction.operand, list):
                operand = ", ".join(f"IL_{target:04x}" for target in instruction.operand)
            elif isinstance(instruction.operand, int) and (
                instruction.is_br() or instruction.is_cond_br() or instruction.is_leave()
            ):
                operand = f"IL_{instruction.operand:04x}"
            else:
                operand = resolver.resolve(instruction.operand)
        print(f"  IL_{instruction.offset:04x}: {instruction.opcode.name:<12} {operand}".rstrip())


def main():
    parser = argparse.ArgumentParser(description="Dump selected managed IL without executing the assembly.")
    parser.add_argument("assembly", type=Path)
    parser.add_argument("method", nargs="+", help="Exact managed method name(s) to dump")
    args = parser.parse_args()

    if not args.assembly.is_file():
        parser.error(f"assembly does not exist: {args.assembly}")

    pe = dnfile.dnPE(str(args.assembly))
    if not pe.net:
        parser.error(f"assembly is not managed: {args.assembly}")

    resolver = MetadataResolver(pe)
    wanted = set(args.method)
    matches = [row for row in pe.net.mdtables.MethodDef.rows if str(row.Name) in wanted]
    found = {str(row.Name) for row in matches}
    missing = sorted(wanted - found)
    if missing:
        print(f"method(s) not found: {', '.join(missing)}", file=sys.stderr)

    for row in matches:
        dump_method(pe, resolver, row)
    return bool(missing)


if __name__ == "__main__":
    raise SystemExit(main())