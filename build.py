"""
author: Nicolas THIBAUT
description: Script to compile the source files into a single distribution file
"""

import argparse
import ast
import os
import re


def filter_body(lines: list, nodes: list) -> str:
    # Delete nodes from bottom to top to preserve the original line numbers.
    for node in sorted(nodes, key=lambda node: node.lineno, reverse=True):
        before = lines[node.lineno - 1][: node.col_offset].strip()
        after = lines[node.end_lineno - 1][node.end_col_offset :].split("#")[0].strip()

        if before or after:
            raise ValueError("Multiple statements on the same line")

        del lines[node.lineno - 1 : node.end_lineno]

    return "\n".join(lines)


def get_local_dependencies(tree, path: str, root: str) -> tuple:
    package = os.path.basename(root)
    dependencies = []
    nodes = []
    # Collect local dependencies
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.name.split(".")[0] == package:
                    raise ValueError(
                        f"Use 'from src.module import ...' for local imports: '{path}'"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                raise ValueError("Specify a module for local import")
            # Resolve local dependency
            if node.level > 0 or node.module.split(".")[0] == package:
                if node not in tree.body:
                    raise ValueError("Local imports must be at top level")
                for name in node.names:
                    if name.asname or name.name == "*":
                        raise ValueError(
                            f"Aliases and wildcards are not supported for local imports: '{path}'"
                        )
                if node.level > 0:
                    # Resolve dots prefix (from ...module import)
                    directory = os.path.dirname(path)
                    for _ in range(node.level - 1):
                        directory = os.path.dirname(directory)
                else:
                    directory = os.path.dirname(root)
                parts = node.module.split(".")
                dependency = os.path.realpath(os.path.join(directory, *parts) + ".py")
                dependencies.append(dependency)
                nodes.append(node)

    return dependencies, nodes


def read_file(path: str, root: str) -> tuple:
    with open(path, encoding="utf-8-sig") as f:
        content = f.read()

    tree = ast.parse(content, filename=path)
    header = None
    lines = content.splitlines()
    dependencies, nodes = get_local_dependencies(tree, path, root)

    # Extract the module docstring
    if ast.get_docstring(tree) is not None:
        node = tree.body[0]
        header = ast.get_source_segment(content, node)
        nodes.append(node)

    return header, filter_body(lines, nodes), dependencies


def collect_sources(path: str, root: str, onhold: tuple = ()) -> dict:
    if path in onhold:
        raise ValueError(f"Circular import for '{path}'")
    if os.path.commonpath([root, path]) != root:
        raise ValueError(f"Local dependency is outside the source directory: '{path}'")

    modules = {}

    header, body, dependencies = read_file(path, root)

    # Pass a new tuple to track the current dependency chain.
    for dependency in dependencies:
        modules.update(collect_sources(dependency, root, (*onhold, path)))

    # Add this file after its dependencies.
    modules[path] = (header, body)

    return modules


def organize_sources(header: str, body: str) -> str:
    tree = ast.parse(body)
    lines = body.splitlines()
    nodes = []
    imports = {}
    variables = {}
    definitions = {}

    for node in tree.body:
        # Parse imports
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # Split multiple imports
            for name in node.names:
                if isinstance(node, ast.Import):
                    statement = ast.Import(names=[name])
                else:
                    statement = ast.ImportFrom(
                        module=node.module,
                        names=[name],
                        level=node.level,
                    )
                signature = ast.dump(statement)
                if signature not in imports:
                    imports[signature] = ast.unparse(statement)
            nodes.append(node)
        # Parse globals
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if hasattr(node, "target"):
                target = node.target
            elif len(node.targets) == 1:
                target = node.targets[0]
            else:
                target = None
            if isinstance(target, ast.Name):
                signature = ast.dump(node)
                if target.id in definitions:
                    if signature != definitions[target.id]:
                        raise ValueError(
                            f"Multiple definitions of '{target.id}' with different declarations"
                        )
                    print(f"[-] Global: duplicate declaration for '{target.id}'")
                nodes.append(node)
                variables[target.id] = ast.get_source_segment(body, node)
                definitions[target.id] = signature

    sections = [header]
    sections.append("\n".join(imports.values()))
    sections.append("\n".join(variables.values()))
    sections.append(filter_body(lines, nodes))

    return re.sub(r"\n{4,}", "\n\n\n", "\n\n\n".join(sections) + "\n")


def bundle_sources(entry: str, source: str, output: str) -> None:
    root = os.path.realpath(source)
    path = os.path.realpath(os.path.join(root, entry))
    modules = collect_sources(path, root)

    header, _ = modules[path]
    if header is None:
        raise ValueError(f"Header metadata not found in '{path}'")
    if os.path.realpath(output) in modules:
        raise ValueError("Output must not overwrite a source file")

    print(header)

    sections = []
    for path, (_, body) in modules.items():
        sections.append(body)
        filename = os.path.relpath(path, root)
        print(f"[+] Source: {filename}")

    content = organize_sources(header, "\n\n\n".join(sections) + "\n")

    # Check syntax
    compile(content, output, "exec")

    # Create destination directory
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)

    with open(output, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"[=] Output: {output}")


def main():
    directory = os.path.dirname(os.path.realpath(__file__))
    parser = argparse.ArgumentParser(description="Build a standalone Open WebUI tool")
    parser.add_argument("--entry", default="main.py")
    parser.add_argument("--source", default=os.path.join(directory, "src"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    try:
        bundle_sources(args.entry, args.source, args.output)
    except (OSError, SyntaxError, ValueError) as e:
        parser.exit(1, f"Build error: {e}\n")


if __name__ == "__main__":
    main()
