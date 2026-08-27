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

        self.assertEqual(
            ["api-budget-design", "three-agent-dev"],
            [package.id for package in packages],
        )
        all_roles = tuple(role for package in packages for role in package.roles)
        self.assertEqual(7, len(all_roles))
        self.assertEqual({"gpt-5.6-sol"}, {role.model for role in all_roles})
        self.assertEqual({"high"}, {role.reasoning_effort for role in all_roles})
        self.assertEqual({"fast"}, {role.service_tier for role in all_roles})
        for package in packages:
            for role in package.roles:
                adapter = render_profile_adapter(package, role)
                self.assertIn('model = "gpt-5.6-sol"\n', adapter)
                self.assertIn('model_reasoning_effort = "high"\n', adapter)
                self.assertIn('service_tier = "fast"\n', adapter)

        package = load_loop_package("three-agent-dev")
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
        self.assertEqual({"high"}, {role.reasoning_effort for role in package.roles})
        self.assertEqual({"fast"}, {role.service_tier for role in package.roles})

    def test_service_tier_is_required_nonempty_role_authority(self) -> None:
        for label, service_tier_line in (
            ("missing", ""),
            ("empty", 'service_tier = ""\n'),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                loops_root = Path(directory)
                package_root = loops_root / "test-loop"
                package_root.mkdir()
                (package_root / "loop.md").write_text("# Test loop\n", encoding="utf-8")
                (package_root / "role.md").write_text("Test role.\n", encoding="utf-8")
                (package_root / "manifest.toml").write_text(
                    """schema_version = 1
id = "test-loop"
manual = "loop.md"

[tmux]
layout = "even-horizontal"
columns = 2
equal_width = true

[[roles]]
id = "role_a"
instructions = "role.md"
runtime_profile = "test-loop-role-a"
model = "gpt-5.6-sol"
reasoning_effort = "high"
"""
                    + service_tier_line
                    + """
[[roles]]
id = "role_b"
instructions = "role.md"
runtime_profile = "test-loop-role-b"
model = "gpt-5.6-sol"
reasoning_effort = "high"
service_tier = "fast"
""",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    LoopPackageError, "service_tier must be a non-empty string"
                ):
                    load_loop_package("test-loop", loops_dir=loops_root)

    def test_api_budget_design_is_a_four_role_single_variable_package(self) -> None:
        package = load_loop_package("api-budget-design")

        self.assertEqual("even-horizontal", package.layout.name)
        self.assertTrue(package.layout.equal_width)
        self.assertEqual(4, package.layout.columns)
        self.assertEqual(
            ["designer_3", "designer_4", "designer_5", "designer_6"],
            [role.id for role in package.roles],
        )
        self.assertEqual(
            [
                "codex-crew-api-budget-design-designer-3",
                "codex-crew-api-budget-design-designer-4",
                "codex-crew-api-budget-design-designer-5",
                "codex-crew-api-budget-design-designer-6",
            ],
            [role.runtime_profile for role in package.roles],
        )
        self.assertEqual({"gpt-5.6-sol"}, {role.model for role in package.roles})
        self.assertEqual({"high"}, {role.reasoning_effort for role in package.roles})
        self.assertEqual({"fast"}, {role.service_tier for role in package.roles})

        normalized_instructions = set()
        language_contract = (
            "user-facing design output 必须使用中文句法",
            "technical identifier、API、module、contract、CLI、schema、file path、error text",
            "不要附加逐段或整篇 English translation",
        )
        for budget, role in zip(range(3, 7), package.roles, strict=True):
            instructions = role.instructions_path.read_text(encoding="utf-8")
            self.assertEqual(1, instructions.count(f"N={budget}"))
            normalized_instructions.add(
                instructions.replace(f"N={budget}", "N=<budget>")
            )
            for heading in (
                "Assumptions",
                "Module map / Deep modules (K <= N)",
                "Public APIs (exactly N)",
                "Main sequential flows",
                "New-build path",
                "Migration path",
                "Discarded abstractions and tradeoffs",
                "Budget audit",
            ):
                self.assertIn(heading, instructions)
            for requirement in language_contract:
                self.assertIn(requirement, instructions)
        self.assertEqual(1, len(normalized_instructions))

        root = Path(__file__).resolve().parents[1]
        public_contracts = (
            package.manual_path.read_text(encoding="utf-8"),
            (root / "README.md").read_text(encoding="utf-8"),
        )
        for contract in public_contracts:
            normalized_contract = " ".join(contract.split())
            for requirement in language_contract:
                self.assertIn(requirement, normalized_contract)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = install_loop_package(
                package,
                codex_home=root / "codex-home",
                managed_root=root / "managed",
            )
            checked = check_loop_installation(
                package,
                codex_home=root / "codex-home",
                managed_root=root / "managed",
            )

        self.assertEqual(installed, checked)
        self.assertEqual(4, len(checked))

    def test_runtime_manual_uses_only_native_thread_control(self) -> None:
        manual = load_loop_package().manual_path.read_text(encoding="utf-8")

        for forbidden in (
            "uv sync",
            "loop install",
            "loop check",
            ".config.toml",
            "symlink",
        ):
            self.assertNotIn(forbidden, manual)
        self.assertNotIn("codex app-server --listen", manual)
        self.assertNotIn("@codex_crew_transport", manual)
        self.assertNotIn("--window", manual)
        self.assertNotIn("--role", manual)
        self.assertIn("--endpoint", manual)
        self.assertIn("--thread-id", manual)
        for command in (
            "crew status",
            "crew send",
            "crew steer",
            "crew wait",
            "crew final",
            "crew goal get",
            "crew goal set",
            "crew goal clear",
        ):
            self.assertIn(command, manual)
        self.assertIn("tmux 只负责可视", manual)
        self.assertIn("Completion comes from `turn/completed`", manual)

    def test_native_migration_has_no_binding_or_tmux_control_residue(self) -> None:
        root = Path(__file__).resolve().parents[1]
        production_paths = [
            *sorted((root / "codex_crew").glob("*.py")),
            root / "README.md",
            root / "DESIGN.md",
            *sorted((root / "loops").glob("*/*.md")),
        ]
        production = "\n".join(
            path.read_text(encoding="utf-8") for path in production_paths
        )
        for forbidden in (
            "crew_thread_binding",
            "prepare_shared_bindings",
            "@codex_crew_db",
            "thread://",
        ):
            self.assertNotIn(forbidden, production)
        runtime_source = (root / "codex_crew" / "crew_runtime.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "send-keys",
            "paste-buffer",
            "load-buffer",
            "capture-pane",
            "set-option",
            "show-options",
        ):
            self.assertNotIn(forbidden, runtime_source)
        launcher_source = (root / "codex_crew" / "launcher.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("resume", launcher_source)
        self.assertFalse((root / "codex_crew" / "crew_control.py").exists())

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
