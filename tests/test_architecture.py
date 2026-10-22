"""Structural checks only. No provider calls, application startup, or business claims."""
from __future__ import annotations

import ast
import importlib
import inspect
import json
from pathlib import Path
import re
import unittest
from typing import get_type_hints

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "atoms_demo"
INDEX = json.loads((ROOT / "docs/contracts-index.json").read_text(encoding="utf-8"))


def module_files() -> dict[str, Path]:
    result = {}
    for path in PACKAGE.rglob("*.py"):
        parts = list(path.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        result[".".join(parts)] = path
    return result


def imports(path: Path, module: str) -> set[str]:
    result = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = node.module or ""
            if node.level:
                parent = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
                prefix = importlib.util.resolve_name("." * node.level + prefix, parent)
            result.add(prefix)
            # Account for `from package import submodule`, not just direct imports.
            result.update(f"{prefix}.{alias.name}" for alias in node.names)
    return {name for name in result if name == "atoms_demo" or name.startswith("atoms_demo.")}


def layer(name: str) -> str:
    parts = name.split(".")
    return parts[1] if len(parts) > 1 else "root"


class ArchitectureTests(unittest.TestCase):
    def test_python_files_parse_and_import_without_startup(self) -> None:
        for name, path in module_files().items():
            with self.subTest(module=name):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
                importlib.import_module(name)
        importlib.import_module("main")

    def test_type_references_resolve(self) -> None:
        for name in module_files():
            module = importlib.import_module(name)
            for item in vars(module).values():
                if inspect.isclass(item) and item.__module__ == name:
                    with self.subTest(module=name, cls=item.__name__):
                        get_type_hints(item)
                        for method in vars(item).values():
                            if inspect.isfunction(method):
                                get_type_hints(method)
                elif inspect.isfunction(item) and item.__module__ == name:
                    get_type_hints(item)

    def test_services_match_contract_signatures(self) -> None:
        for name, spec in INDEX["modules"].items():
            contract = getattr(importlib.import_module(f"atoms_demo.contracts.{name}"), spec["protocol"])
            service = getattr(importlib.import_module(f"atoms_demo.services.{name}"), spec["service"])
            for method_name, method in vars(contract).items():
                if method_name.startswith("_") or not inspect.isfunction(method):
                    continue
                with self.subTest(module=name, method=method_name):
                    implementation = getattr(service, method_name)
                    self.assertEqual(inspect.signature(method), inspect.signature(implementation))
                    self.assertEqual(get_type_hints(method), get_type_hints(implementation))
                    self.assertTrue(inspect.iscoroutinefunction(implementation))

    def test_dependency_layers_and_service_ownership(self) -> None:
        allowed = {
            "core": {"core"},
            "contracts": {"core", "contracts"},
            "ports": {"core", "contracts", "ports"},
            "services": {"core", "contracts", "ports"},
            "workflows": {"core", "contracts", "ports", "workflows"},
            "adapters": {"core", "contracts", "ports", "config", "adapters"},
            "api": {"core", "contracts", "workflows", "api", "config"},
            "config": {"core"},
            "bootstrap": {"core", "config", "contracts", "ports", "services",
                          "workflows", "adapters", "api"},
            "root": set(),
        }
        files = module_files()
        for source, path in files.items():
            for target in imports(path, source):
                # Drop imported symbols, retaining exact known Python modules.
                if target not in files:
                    continue
                with self.subTest(source=source, target=target):
                    self.assertIn(layer(target), allowed[layer(source)])
                    if layer(source) == "services" and layer(target) == "contracts":
                        self.assertEqual(source.split(".")[-1], target.split(".")[-1])
                    if layer(source) == "contracts" and layer(target) == "contracts":
                        self.assertEqual(source, target, "Contract must not depend on sibling contract")

    def test_import_graph_has_no_cycles(self) -> None:
        files = module_files()
        graph = {name: imports(path, name) & files.keys() for name, path in files.items()}
        visited: set[str] = set()
        active: list[str] = []

        def visit(name: str) -> None:
            if name in active:
                self.fail("Import cycle: " + " -> ".join(active + [name]))
            if name in visited:
                return
            active.append(name)
            for target in graph[name]:
                visit(target)
            active.pop()
            visited.add(name)

        for name in graph:
            visit(name)

    def test_routes_have_unique_paths_and_real_interface_targets(self) -> None:
        from atoms_demo.api.application import Application
        from atoms_demo.api.routes import ROUTES
        from atoms_demo.workflows.registry import Services

        service_types = get_type_hints(Services)
        workflow_types = get_type_hints(Application)
        keys = set()
        for route in ROUTES:
            with self.subTest(route=route):
                key = (route.method, route.path)
                self.assertNotIn(key, keys)
                keys.add(key)
                owner = (workflow_types if route.workflow else service_types)[route.owner]
                self.assertTrue(callable(getattr(owner, route.operation)))
                self.assertTrue(route.path.startswith("/"))
                self.assertIn(route.method, {"GET", "POST", "PUT", "PATCH", "DELETE"})

    def test_design_modules_and_functional_ids_are_covered(self) -> None:
        design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
        for module in INDEX["readme_modules"]:
            self.assertIn(module, design)
        functional_ids = set(re.findall(r"^\| (F\d{2}) \|", design, re.MULTILINE))
        self.assertEqual(functional_ids, set(INDEX["functional_requirements"]))
        for group in ("readme_modules", "functional_requirements"):
            for requirement, owners in INDEX[group].items():
                self.assertTrue(owners, requirement)
                for owner in owners:
                    self.assertIn(owner, INDEX["modules"])
                    self.assertTrue((PACKAGE / "services" / f"{owner}.py").is_file())

    def test_main_only_orchestrates(self) -> None:
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        calls = [node for node in ast.walk(main) if isinstance(node, ast.Call)]
        self.assertEqual([node.func.id for node in calls if isinstance(node.func, ast.Name)],
                         ["load_settings", "compose_application", "serve"])
        self.assertEqual(len(main.body), 3)
        forbidden = (ast.For, ast.While, ast.Try, ast.If, ast.With, ast.AsyncWith)
        self.assertFalse(any(isinstance(node, forbidden) for node in ast.walk(main)))

    def test_frontend_features_do_not_import_each_other(self) -> None:
        for path in (ROOT / "frontend/src/features").rglob("*.ts*"):
            text = path.read_text(encoding="utf-8")
            for target in re.findall(r'''from\s+["']([^"']+)["']''', text):
                if target.startswith("."):
                    resolved = (path.parent / target).resolve()
                    features = (ROOT / "frontend/src/features").resolve()
                    if resolved.is_relative_to(features):
                        self.assertEqual(resolved.relative_to(features).parts[0],
                                         path.relative_to(features).parts[0])
                self.assertNotIn("@/features/", target)

    def test_only_infrastructure_execution_is_deferred(self) -> None:
        self.assertEqual(INDEX["deferred_only"], ["atoms_demo.adapters.infrastructure"])
        design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
        self.assertIn("其他 TODO 仍属于必须完成项", design)


if __name__ == "__main__":
    unittest.main()
