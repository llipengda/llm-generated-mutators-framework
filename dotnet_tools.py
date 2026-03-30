import clr
import os
import sys

from System.Reflection import BindingFlags, MemberTypes # type: ignore

class MultiAssemblyInspector:
    def __init__(self, target_paths: list):
        self.all_types = []
        self.loaded_assemblies = []
        
        for path in target_paths:
            if os.path.isdir(path):
                for file in os.listdir(path):
                    if file.endswith(".dll"):
                        self._load_assembly(os.path.join(path, file))
            elif os.path.isfile(path) and path.endswith(".dll"):
                self._load_assembly(path)

    def _load_assembly(self, dll_path: str):
        try:
            full_path = os.path.abspath(dll_path)
            dll_dir = os.path.dirname(full_path)
            if dll_dir not in sys.path:
                sys.path.append(dll_dir)
            
            assembly = clr.AddReference(full_path) # type: ignore
            self.loaded_assemblies.append(assembly)
            
            exported_types = assembly.GetExportedTypes()
            self.all_types.extend(exported_types)
            print(f"Successfully loaded: {os.path.basename(dll_path)} ({len(exported_types)} types)")
        except Exception as e:
            print(f"Skipped {os.path.basename(dll_path)}: {str(e)[:80]}...")

    def _format_type_name(self, t) -> str:
        if t is None: return "void"
        if not hasattr(t, "IsGenericType") or not t.IsGenericType:
            return t.Name
        try:
            args = ", ".join([arg.Name for arg in t.GetGenericArguments()])
            base_name = t.Name.split('`')[0]
            return f"{base_name}<{args}>"
        except:
            return t.Name

    def _get_inheritance_info(self, target_type) -> list:
        """Extracts the base class hierarchy and implemented interfaces."""
        info = []
        
        # 1. Base Class Hierarchy
        hierarchy = []
        current = target_type.BaseType
        while current:
            hierarchy.append(current.FullName or current.Name)
            current = current.BaseType
        
        if hierarchy:
            info.append(f"**Inheritance**: {' -> '.join(hierarchy)}")
        
        # 2. Interfaces
        interfaces = target_type.GetInterfaces()
        if interfaces:
            if len(interfaces) > 5:
                iface_names = [i.Name for i in interfaces[:5]] + ["..."]
            else:
                iface_names = [i.Name for i in interfaces]
            info.append(f"**Implements**: {', '.join(iface_names)}")
            
        return info

    def _get_member_sig(self, m) -> str:
        try:
            if m.MemberType == MemberTypes.Method:
                params = ", ".join([f"{self._format_type_name(p.ParameterType)} {p.Name}" for p in m.GetParameters()])
                return f"{self._format_type_name(m.ReturnType)} {m.Name}({params})"
            elif m.MemberType == MemberTypes.Property:
                access = []
                if m.CanRead: access.append("get;")
                if m.CanWrite: access.append("set;")
                return f"{self._format_type_name(m.PropertyType)} {m.Name} {{ {' '.join(access)} }}"
            elif m.MemberType == MemberTypes.Field:
                return f"{self._format_type_name(m.FieldType)} {m.Name}"
            elif m.MemberType == MemberTypes.Constructor:
                params = ", ".join([f"{self._format_type_name(p.ParameterType)} {p.Name}" for p in m.GetParameters()])
                return f".ctor({params})"
        except:
            return f"{m.Name} (Parsing failed)"
        return m.Name

    def fuzzy_search(self, query: str) -> str:
        query = query.lower()
        matches = [t for t in self.all_types if query in t.FullName.lower()]
        
        if not matches:
            return f"No matches for '{query}'."
        
        if len(matches) > 1:
            res = [f"Found {len(matches)} matches:"]
            for t in matches[:15]:
                res.append(f"- {t.FullName} ({t.Assembly.GetName().Name})")
            return "\n".join(res)

        target = matches[0]
        flags = BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly
        members = target.GetMembers(flags)
        
        # Build Output
        output = [
            f"## Class: {target.FullName}",
            f"**Assembly**: {target.Assembly.GetName().Name}"
        ]
        
        # Add Inheritance Info
        output.extend(self._get_inheritance_info(target))
        output.append("-" * 50)
        
        # Add Members
        for m in sorted(members, key=lambda x: str(x.MemberType)):
            sig = self._get_member_sig(m)
            output.append(f"[{str(m.MemberType):<12}] {sig}")
            
        return "\n".join(output)

if not os.path.exists("./peach/sdk/"):
    print("Error: ./peach/sdk/ not found. Please run `./setup.sh peach` first to prepare the SDK.")
    sys.exit(1)

inspector = MultiAssemblyInspector(["./peach/sdk/"])

from langchain_core.tools import tool
from log import console, file_logger

@tool("Search_Class")
def search_class(query: str) -> str:
    """Search for a class and its members in the loaded assemblies."""
    console.log(f"[dim]Tool: Searching for class matching '{query}'...[/dim]")
    file_logger.log(
f"""TOOL CALL: search_class
    query: {query}
""")
    response = inspector.fuzzy_search(query)
    file_logger.log(
f"""TOOL RESPONSE:
{response}
""")
    return response