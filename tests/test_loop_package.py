from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_crew.loop_package import (
    LoopPackageError,
    check_loop_installation,
    discover_loop_packages,
    install_loop_package,
    load_loop_package,
    render_profile_adapter,
)


class LoopPackageTests(unittest.TestCase):
    def test_manifest_is_complete_runtime_authority(self) -> None:
        packages = discover_loop_packages()

        self.assertEqual(["three-agent-dev"], [package.id for package in packages])
        package = packages[0]
        self.assertEqual("loop.md", package.manual_path.name)
        self.assertEqual("even-horizontal", package.layout.name)
        self.assertTrue(package.layout.equal_width)
        self.assertEqual(3, package.layout.columns)
        self.assertEqual(
            ["commander", "worker", "judger"],
            [role.id for role in package.roles],
        )
        self.assertEqual(
            [
                "codex-crew-three-agent-dev-commander",
                "codex-crew-three-agent-dev-worker",
                "codex-crew-three-agent-dev-judger",
            ],
            [role.runtime_profile for role in package.roles],
        )
        self.assertEqual({"gpt-5.6-sol"}, {role.model for role in package.roles})
        self.assertEqual({"xhigh"}, {role.reasoning_effort for role in package.roles})

    def test_runtime_manual_excludes_operator_bootstrap(self) -> None:
        manual = load_loop_package().manual_path.read_text(encoding="utf-8")

        for forbidden in (
            "uv sync",
            "loop install",
            "loop check",
            ".config.toml",
            "symlink",
        ):
            self.assertNotIn(forbidden, manual)
        self.assertIn("由 operator 在 session 启动前外部", manual)

    def test_install_is_deterministic_idempotent_and_checkable(self) -> None:
        package = load_loop_package()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            managed_root = root / "managed"

            first = install_loop_package(
                package, codex_home=codex_home, managed_root=managed_root
            )
            contents = {
                item.runtime_profile: item.adapter_path.read_bytes() for item in first
            }
            second = install_loop_package(
                package, codex_home=codex_home, managed_root=managed_root
            )
            checked = check_loop_installation(
                package, codex_home=codex_home, managed_root=managed_root
            )

            self.assertEqual(first, second)
            self.assertEqual(first, checked)
            for item in checked:
                self.assertTrue(item.symlink_path.is_symlink())
                self.assertEqual(
                    item.adapter_path.resolve(), item.symlink_path.resolve()
                )
                self.assertEqual(
                    contents[item.runtime_profile], item.adapter_path.read_bytes()
                )
                role = next(
                    role for role in package.roles if role.id == item.role
                )
                self.assertEqual(
                    render_profile_adapter(package, role),
                    item.adapter_path.read_text(encoding="utf-8"),
                )

    def test_install_preflight_fails_closed_on_runtime_conflicts(self) -> None:
        package = load_loop_package()
        first_role = package.roles[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            managed_root = root / "managed"
            codex_home.mkdir()
            conflict = codex_home / f"{first_role.runtime_profile}.config.toml"
            conflict.write_text("user-owned\n", encoding="utf-8")

            with self.assertRaisesRegex(LoopPackageError, "not a symlink"):
                install_loop_package(
                    package, codex_home=codex_home, managed_root=managed_root
                )

            self.assertFalse(managed_root.exists())
            self.assertEqual("user-owned\n", conflict.read_text(encoding="utf-8"))

    def test_install_preflight_rejects_wrong_symlink_and_unmanaged_adapter(self) -> None:
        package = load_loop_package()
        first_role = package.roles[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            managed_root = root / "managed"
            codex_home.mkdir()
            runtime_target = (
                codex_home / f"{first_role.runtime_profile}.config.toml"
            )
            runtime_target.symlink_to(root / "wrong.config.toml")

            with self.assertRaisesRegex(LoopPackageError, "wrong target"):
                install_loop_package(
                    package, codex_home=codex_home, managed_root=managed_root
                )

            self.assertFalse(managed_root.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            managed_root = root / "managed"
            adapter = (
                managed_root
                / package.id
                / f"{first_role.runtime_profile}.config.toml"
            )
            adapter.parent.mkdir(parents=True)
            adapter.write_text("user-owned\n", encoding="utf-8")

            with self.assertRaisesRegex(LoopPackageError, "not managed"):
                install_loop_package(
                    package, codex_home=codex_home, managed_root=managed_root
                )

            self.assertFalse(codex_home.exists())
            self.assertEqual("user-owned\n", adapter.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
