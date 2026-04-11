import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RACELINK_ROOT = ROOT / "racelink"


def _module_name_for(path: pathlib.Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(module_name: str, level: int, target: str | None) -> str:
    parts = module_name.split(".")
    base = parts[:-level]
    if target:
        base.extend(target.split("."))
    return ".".join(base)


def _import_targets(path: pathlib.Path) -> list[str]:
    module_name = _module_name_for(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                targets.append(_resolve_relative(module_name, node.level, node.module))
            elif node.module:
                targets.append(node.module)
    return targets


def _iter_python_files(root: pathlib.Path):
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


class ArchitectureImportTests(unittest.TestCase):
    def _assert_no_forbidden_imports(self, package_rel: str, forbidden_prefixes: tuple[str, ...]):
        package_root = RACELINK_ROOT / package_rel
        for path in _iter_python_files(package_root):
            imports = _import_targets(path)
            bad = [name for name in imports if name and name.startswith(forbidden_prefixes)]
            self.assertEqual(
                bad,
                [],
                msg=f"{path.relative_to(ROOT)} imports forbidden modules: {bad}",
            )

    def test_domain_does_not_import_flask_or_rotorhazard(self):
        self._assert_no_forbidden_imports(
            "domain",
            (
                "flask",
                "RHUI",
                "eventmanager",
                "racelink.integrations.rotorhazard",
            ),
        )

    def test_transport_does_not_import_rotorhazard(self):
        self._assert_no_forbidden_imports(
            "transport",
            (
                "RHUI",
                "eventmanager",
                "racelink.integrations.rotorhazard",
            ),
        )

    def test_services_do_not_import_rotorhazard_ui(self):
        self._assert_no_forbidden_imports(
            "services",
            (
                "RHUI",
                "eventmanager",
                "racelink.integrations.rotorhazard",
            ),
        )

    def test_core_layers_do_not_import_rotorhazard_integration(self):
        for package_rel in ("core", "domain", "protocol", "transport", "state", "services"):
            self._assert_no_forbidden_imports(
                package_rel,
                ("racelink.integrations.rotorhazard",),
            )


if __name__ == "__main__":
    unittest.main()
