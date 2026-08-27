from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_crew.loop_package import (
    EvenHorizontalTmuxLayout,
    LoopPackageError,
    SplitPlanTmuxLayout,
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
        self.assertEqual(8, len(all_roles))
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
        self.assertIsInstance(package.layout, EvenHorizontalTmuxLayout)
        self.assertEqual("loop.md", package.manual_path.name)
        self.assertEqual("even-horizontal", package.layout.name)
        self.assertTrue(package.layout.equal_width)
        self.assertEqual(3, package.layout.columns)
        self.assertEqual(
            ["commander", "worker", "judger"],
            [role.id for role in package.roles],
        )
        self.assertEqual("commander", package.communication_role)
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
        commander = package.roles[0].instructions_path.read_text(encoding="utf-8")
        for requirement in (
            "Required per-round accounting is native goal",
            "A model token observation is optional",
            "exact `crew wait` result",
            "Missing model usage never blocks the round",
            "Never subtract an unobserved baseline",
            "`cachedInputTokens`",
        ):
            self.assertIn(requirement, commander)

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
                    """schema_version = 2
id = "test-loop"
manual = "loop.md"
communication_role = "role_a"

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

    def test_manifest_requires_exactly_one_valid_communication_role(self) -> None:
        variants = {
            "missing": ("", "communication_role must be a non-empty string"),
            "invalid": (
                'communication_role = "Invalid Role"\n',
                "invalid communication_role",
            ),
            "non-member": (
                'communication_role = "observer"\n',
                "must reference exactly one ordered role",
            ),
            "duplicate": (
                'communication_role = "role_a"\ncommunication_role = "role_b"\n',
                "cannot read loop manifest",
            ),
        }
        for label, (communication_line, expected) in variants.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                loops_root = Path(directory)
                package_root = loops_root / "test-loop"
                package_root.mkdir()
                (package_root / "loop.md").write_text("# Test loop\n", encoding="utf-8")
                (package_root / "role.md").write_text("Test role.\n", encoding="utf-8")
                (package_root / "manifest.toml").write_text(
                    """schema_version = 2
id = "test-loop"
manual = "loop.md"
"""
                    + communication_line
                    + """
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
service_tier = "fast"

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

                with self.assertRaisesRegex(LoopPackageError, expected):
                    load_loop_package("test-loop", loops_dir=loops_root)

    def test_split_plan_rejects_invalid_or_non_acyclic_steps(self) -> None:
        variants = {
            "incomplete": (
                """[[tmux.steps]]
role = "role_b"
target = "role_a"
direction = "horizontal"
percentage = 50
""",
                "must cover every non-root role",
            ),
            "forward": (
                """[[tmux.steps]]
role = "role_b"
target = "role_c"
direction = "horizontal"
percentage = 50

[[tmux.steps]]
role = "role_c"
target = "role_a"
direction = "vertical"
percentage = 50
""",
                "must reference an earlier role",
            ),
            "cyclic": (
                """[[tmux.steps]]
role = "role_b"
target = "role_c"
direction = "horizontal"
percentage = 50

[[tmux.steps]]
role = "role_c"
target = "role_b"
direction = "vertical"
percentage = 50
""",
                "must reference an earlier role",
            ),
            "wrong-order": (
                """[[tmux.steps]]
role = "role_c"
target = "role_a"
direction = "horizontal"
percentage = 50

[[tmux.steps]]
role = "role_b"
target = "role_a"
direction = "vertical"
percentage = 50
""",
                "must create ordered role",
            ),
            "invalid-direction": (
                """[[tmux.steps]]
role = "role_b"
target = "role_a"
direction = "diagonal"
percentage = 50

[[tmux.steps]]
role = "role_c"
target = "role_b"
direction = "vertical"
percentage = 50
""",
                "direction must be horizontal or vertical",
            ),
            "invalid-percentage": (
                """[[tmux.steps]]
role = "role_b"
target = "role_a"
direction = "horizontal"
percentage = 100

[[tmux.steps]]
role = "role_c"
target = "role_b"
direction = "vertical"
percentage = 50
""",
                "percentage must be an integer from 1 through 99",
            ),
        }
        roles = """
[[roles]]
id = "role_a"
instructions = "role.md"
runtime_profile = "test-loop-role-a"
model = "gpt-5.6-sol"
reasoning_effort = "high"
service_tier = "fast"

[[roles]]
id = "role_b"
instructions = "role.md"
runtime_profile = "test-loop-role-b"
model = "gpt-5.6-sol"
reasoning_effort = "high"
service_tier = "fast"

[[roles]]
id = "role_c"
instructions = "role.md"
runtime_profile = "test-loop-role-c"
model = "gpt-5.6-sol"
reasoning_effort = "high"
service_tier = "fast"
"""
        for label, (steps, expected) in variants.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                loops_root = Path(directory)
                package_root = loops_root / "test-loop"
                package_root.mkdir()
                (package_root / "loop.md").write_text("# Test loop\n", encoding="utf-8")
                (package_root / "role.md").write_text("Test role.\n", encoding="utf-8")
                (package_root / "manifest.toml").write_text(
                    """schema_version = 2
id = "test-loop"
manual = "loop.md"
communication_role = "role_a"

[tmux]
layout = "split-plan"

"""
                    + steps
                    + roles,
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(LoopPackageError, expected):
                    load_loop_package("test-loop", loops_dir=loops_root)

    def test_api_budget_design_is_a_five_role_exact_budget_package(self) -> None:
        package = load_loop_package("api-budget-design")

        self.assertIsInstance(package.layout, SplitPlanTmuxLayout)
        self.assertEqual("split-plan", package.layout.name)
        self.assertEqual(
            [
                ("worker_3", "commander", "horizontal", 67),
                ("worker_4", "worker_3", "vertical", 50),
                ("worker_5", "worker_3", "horizontal", 50),
                ("worker_6", "worker_4", "horizontal", 50),
            ],
            [
                (step.role, step.target, step.direction, step.percentage)
                for step in package.layout.steps
            ],
        )
        self.assertEqual("commander", package.communication_role)
        self.assertEqual(
            ["commander", "worker_3", "worker_4", "worker_5", "worker_6"],
            [role.id for role in package.roles],
        )
        self.assertEqual(
            [
                "codex-crew-api-budget-design-commander",
                "codex-crew-api-budget-design-worker-3",
                "codex-crew-api-budget-design-worker-4",
                "codex-crew-api-budget-design-worker-5",
                "codex-crew-api-budget-design-worker-6",
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
        worker_instructions = []
        for budget, role in zip(range(3, 7), package.roles[1:], strict=True):
            instructions = role.instructions_path.read_text(encoding="utf-8")
            worker_instructions.append(instructions)
            self.assertEqual(1, instructions.count(f"N={budget}"))
            normalized_instructions.add(
                instructions.replace(f"N={budget}", "N=<budget>")
            )
            for heading in (
                "Assumptions",
                "Module map / Deep modules (exactly N)",
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
            self.assertIn("Use exactly N deep modules", instructions)
            self.assertIn("deep_modules=N/N", instructions)
            self.assertIn("public_apis=N/N", instructions)
        self.assertEqual(1, len(normalized_instructions))

        commander = package.roles[0].instructions_path.read_text(encoding="utf-8")
        for requirement in (
            "only user-facing communication role",
            "runtime control envelope",
            "byte-identical original design request",
            "Required per-round accounting is each native goal",
            "A model token observation is optional",
            "exact `crew wait` result",
            "Missing model usage never blocks comparison or the round",
            "Never subtract an unobserved baseline",
            "cachedInputTokens",
            "timeUsedSeconds",
            "round wall elapsed",
            "Do not start another round",
            "Do not read from or modify the target worktree",
        ):
            self.assertIn(requirement, commander)

        root = Path(__file__).resolve().parents[1]
        public_contracts = (
            package.manual_path.read_text(encoding="utf-8"),
            (root / "README.md").read_text(encoding="utf-8"),
        )
        readme = public_contracts[1]
        for requirement in (
            "loop package** 是 repository-owned artifact",
            "`package` 不是 CLI subcommand",
            "不存在 `codex-crew loop package`",
            "./bin/codex-crew loop list --json",
            "./bin/codex-crew loop install api-budget-design",
            "./bin/codex-crew loop check api-budget-design",
        ):
            self.assertIn(requirement, readme)
        for contract in public_contracts:
            normalized_contract = " ".join(contract.split())
            for requirement in language_contract:
                self.assertIn(requirement, normalized_contract)

        budget_contracts = (*worker_instructions, commander, *public_contracts)
        forbidden_budget_phrases = (
            "at " "most N deep modules",
            "K <" "= N",
            "K" "/N",
        )
        for contract in budget_contracts:
            for forbidden in forbidden_budget_phrases:
                self.assertNotIn(forbidden, contract)

        for contract in public_contracts:
            for requirement in (
                "worker_3 ->",
                "worker_6 ->",
                "commander ->",
                "communication role",
                "runtime handoff",
            ):
                self.assertIn(requirement, contract)

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
        self.assertEqual(5, len(checked))

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
        external_close_command = "`codex-crew crew close --window-id @N`"
        self.assertEqual(1, manual.count(external_close_command))
        native_dispatch = manual.split(
            "之后只有 Commander 接收用户 task/request。", 1
        )[1].split("## One acceptance loop", 1)[0]
        self.assertNotIn("--window", native_dispatch)
        self.assertNotIn("--role", native_dispatch)
        command_blocks = tuple(
            part.split("```", 1)[0]
            for part in native_dispatch.split("```bash\n")[1:]
        )
        commands = tuple(
            line.strip()
            for block in command_blocks
            for line in block.replace("\\\n", "").splitlines()
            if line.strip().startswith("codex-crew crew ")
        )
        expected_commands = (
            "crew status",
            "crew send",
            "crew wait",
            "crew final",
            "crew steer",
            "crew goal get",
            "crew goal set",
            "crew goal clear",
        )
        self.assertEqual(len(expected_commands), len(commands))
        for expected, command in zip(expected_commands, commands, strict=True):
            self.assertTrue(command.startswith(f"codex-crew {expected} "))
            self.assertIn('--endpoint "$ENDPOINT"', command)
            self.assertIn('--thread-id "$WORKER_THREAD"', command)
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

    def test_install_removes_obsolete_managed_profile_after_role_migration(self) -> None:
        package = load_loop_package("api-budget-design")
        obsolete_profiles = (
            "codex-crew-api-budget-design-coordinator",
            "codex-crew-api-budget-design-designer-3",
            "codex-crew-api-budget-design-designer-4",
            "codex-crew-api-budget-design-designer-5",
            "codex-crew-api-budget-design-designer-6",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            managed_root = root / "managed"
            install_loop_package(
                package, codex_home=codex_home, managed_root=managed_root
            )
            obsolete_adapters = []
            for profile in obsolete_profiles:
                obsolete_adapter = (
                    managed_root / package.id / f"{profile}.config.toml"
                )
                obsolete_adapter.write_text(
                    "# @generated by codex-crew loop adapter v1\nold\n",
                    encoding="utf-8",
                )
                obsolete_symlink = codex_home / obsolete_adapter.name
                obsolete_symlink.symlink_to(obsolete_adapter)
                obsolete_adapters.append((obsolete_adapter, obsolete_symlink))

            with self.assertRaisesRegex(
                LoopPackageError, "obsolete managed adapters remain"
            ):
                check_loop_installation(
                    package, codex_home=codex_home, managed_root=managed_root
                )
            install_loop_package(
                package, codex_home=codex_home, managed_root=managed_root
            )

            for obsolete_adapter, obsolete_symlink in obsolete_adapters:
                self.assertFalse(obsolete_adapter.exists())
                self.assertFalse(obsolete_symlink.is_symlink())
            self.assertEqual(
                5,
                len(
                    check_loop_installation(
                        package,
                        codex_home=codex_home,
                        managed_root=managed_root,
                    )
                ),
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
