from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bugflow_artifacts as artifacts  # noqa: E402
import bugflow_runner as runner  # noqa: E402
import normalize_issue_payload as normalizer  # noqa: E402


@contextlib.contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def complete_evidence(*findings: str) -> dict[str, object]:
    return {
        "status": "complete",
        "detail": "complete",
        "comments": "complete",
        "activities": "complete",
        "media": "complete",
        "fetched_at": "2026-07-13T10:00:00+08:00",
        "findings": list(findings),
        "missing": [],
    }


class ConfigSafetyTests(unittest.TestCase):
    def test_local_override_cannot_turn_project_false_permissions_true(self) -> None:
        project = {
            "remote_status_policy": {
                "update_status_allowed": False,
                "update_comments_allowed": False,
                "default_change_to_in_progress": False,
            },
            "execution_policy": {
                "auto_fix_allowed": False,
                "auto_fix_low_risk_frontend": False,
                "allow_lightweight_verification": False,
            },
            "git_policy": {
                "auto_commit_after_fix": False,
                "push_after_commit": False,
            },
        }
        local = {
            "remote_status_policy": {
                "update_status_allowed": True,
                "update_comments_allowed": True,
                "default_change_to_in_progress": True,
            },
            "execution_policy": {
                "auto_fix_allowed": True,
                "auto_fix_low_risk_frontend": True,
                "allow_lightweight_verification": True,
            },
            "git_policy": {
                "auto_commit_after_fix": True,
                "push_after_commit": True,
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "project.yaml"
            local_path = Path(temp_dir) / "local.yaml"
            project_path.write_text("project\n", encoding="utf-8")
            local_path.write_text("local\n", encoding="utf-8")

            def fake_load(path: Path) -> dict[str, object]:
                return copy.deepcopy(project if path == project_path else local)

            with mock.patch.object(runner, "load_yaml", side_effect=fake_load):
                config = runner.load_config(project_path, local_path)

        for section, keys in {
            "remote_status_policy": (
                "update_status_allowed",
                "update_comments_allowed",
                "default_change_to_in_progress",
            ),
            "execution_policy": (
                "auto_fix_allowed",
                "auto_fix_low_risk_frontend",
                "allow_lightweight_verification",
            ),
            "git_policy": ("auto_commit_after_fix", "push_after_commit"),
        }.items():
            for key in keys:
                self.assertIs(
                    config[section][key],
                    False,
                    f"local override loosened {section}.{key}",
                )

    def test_local_override_can_make_project_permission_stricter(self) -> None:
        project = {"remote_status_policy": {"update_comments_allowed": True}}
        local = {"remote_status_policy": {"update_comments_allowed": False}}

        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "project.yaml"
            local_path = Path(temp_dir) / "local.yaml"
            project_path.write_text("project\n", encoding="utf-8")
            local_path.write_text("local\n", encoding="utf-8")
            with mock.patch.object(
                runner,
                "load_yaml",
                side_effect=lambda path: copy.deepcopy(
                    project if path == project_path else local
                ),
            ):
                config = runner.load_config(project_path, local_path)

        self.assertIs(config["remote_status_policy"]["update_comments_allowed"], False)


class SkillTemplatePolicyTests(unittest.TestCase):
    def test_feishu_starter_enables_only_normal_repair_status_transitions(self) -> None:
        config = runner.load_yaml(SKILL_ROOT / "assets/feishu-project-config.template.yaml")
        policy = config["remote_status_policy"]

        self.assertIs(policy["update_status_allowed"], True)
        self.assertIs(policy["default_change_to_in_progress"], True)
        self.assertIs(policy["default_resolve_for_acceptance"], True)
        self.assertIs(policy["update_comments_allowed"], False)
        self.assertIs(policy["default_complete"], False)
        self.assertIs(policy["default_terminate"], False)

    def test_exported_json_starter_keeps_remote_actions_disabled(self) -> None:
        config = runner.load_yaml(SKILL_ROOT / "assets/project-config.template.yaml")
        policy = config["remote_status_policy"]

        self.assertFalse(any(bool(value) for value in policy.values()))

    def test_starters_enable_plan_bound_lightweight_completion_policy(self) -> None:
        feishu = runner.load_yaml(SKILL_ROOT / "assets/feishu-project-config.template.yaml")
        exported = runner.load_yaml(SKILL_ROOT / "assets/project-config.template.yaml")

        self.assertIs(feishu["execution_policy"]["allow_lightweight_verification"], True)
        self.assertEqual(
            feishu["execution_policy"]["approved_completion_actions"],
            ["commit", "start-fix", "resolve-for-acceptance"],
        )
        self.assertIs(exported["execution_policy"]["allow_lightweight_verification"], True)
        self.assertEqual(exported["execution_policy"]["approved_completion_actions"], ["commit"])
        self.assertEqual(exported["query_policy"]["assigned_to"], "")

    def test_browser_starters_prefer_existing_chrome_without_auth_copying(self) -> None:
        expected_surfaces = [
            "existing_chrome_tab",
            "existing_in_app_browser_tab",
            "new_in_app_browser_tab",
        ]
        expected_login_prefix = [
            "existing_chrome_session",
            "existing_in_app_browser_session",
        ]
        for asset in (
            "feishu-project-config.template.yaml",
            "project-config.template.yaml",
            "local-overrides.template.yaml",
        ):
            with self.subTest(asset=asset):
                config = runner.load_yaml(SKILL_ROOT / "assets" / asset)
                self.assertEqual(
                    config["browser_verification"]["surface_priority"],
                    expected_surfaces,
                )
                self.assertEqual(
                    config["login_policy"]["method_priority"][:2],
                    expected_login_prefix,
                )

        reference = (SKILL_ROOT / "references/browser-verification.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Reuse the matching tab", reference)
        self.assertIn("Never inspect, export, copy, or inject browser cookies", reference)
        self.assertIn("Do not migrate authentication data", reference)


class TriageAndRepairGateTests(unittest.TestCase):
    MATCH = {
        "repository_match": "current-repo",
        "confidence": "high",
        "confirmation_required": False,
        "customer_confirmation_question": "",
    }

    @staticmethod
    def config(*, auto_fix: bool = True, low_risk_frontend: bool = True) -> dict[str, object]:
        return {
            "project": {"role_assumption": "frontend"},
            "execution_policy": {
                "auto_fix_allowed": auto_fix,
                "auto_fix_low_risk_frontend": low_risk_frontend,
                "max_auto_fix_effort": "medium",
            },
        }

    def test_current_repo_functional_bug_defaults_to_manual_review(self) -> None:
        issue = {
            "id": "1",
            "number": "BUG-1",
            "title": "订单保存后数据丢失",
            "description": "用户点击保存后，刚填写的业务数据消失。",
            "evidence_fetch": complete_evidence(),
        }

        result = runner.classify_issue(self.config(), issue, self.MATCH)

        self.assertEqual(result["readiness"], "manual-review-first")
        self.assertNotEqual(result["readiness"], "auto-fix-candidate")

    def test_pure_frontend_low_risk_bug_is_auto_only_when_both_policies_allow_it(self) -> None:
        issue = {
            "id": "2",
            "number": "BUG-2",
            "title": "列表页按钮颜色与设计稿不一致",
            "description": "只调整独立页面按钮的 CSS 颜色值。",
            "evidence_fetch": complete_evidence(),
        }

        allowed = runner.classify_issue(self.config(), issue, self.MATCH)
        auto_fix_disabled = runner.classify_issue(
            self.config(auto_fix=False), issue, self.MATCH
        )
        frontend_auto_fix_disabled = runner.classify_issue(
            self.config(low_risk_frontend=False), issue, self.MATCH
        )

        self.assertEqual(allowed["readiness"], "auto-fix-candidate")
        self.assertEqual(auto_fix_disabled["readiness"], "manual-review-first")
        self.assertEqual(frontend_auto_fix_disabled["readiness"], "manual-review-first")

    def test_approved_does_not_bypass_unresolved_confirmation(self) -> None:
        item = {
            "repository_match": "current-repo",
            "ownership": "needs-confirmation",
            "confirmation_required": True,
            "readiness": "ask-for-confirmation",
            "effort": "easy",
        }

        status, blockers = runner.repair_gate(self.config(), item, approved=True)

        self.assertEqual(status, "blocked")
        self.assertTrue(blockers)
        self.assertRegex(" ".join(blockers).lower(), r"confirm|ownership")

    def test_approved_does_not_bypass_other_repository_ownership(self) -> None:
        item = {
            "repository_match": "other-repo",
            "ownership": "not-current-repo",
            "confirmation_required": False,
            "readiness": "manual-review-first",
            "effort": "easy",
        }

        status, blockers = runner.repair_gate(self.config(), item, approved=True)

        self.assertEqual(status, "blocked")
        self.assertTrue(blockers)
        self.assertRegex(" ".join(blockers).lower(), r"ownership|repository|current repo")

    def test_plan_approval_can_bypass_auto_fix_capability_being_disabled(self) -> None:
        config = self.config(auto_fix=False, low_risk_frontend=False)
        config["_bugflow_safety"] = {
            "local_denies": ["execution_policy.auto_fix_allowed"]
        }
        item = {
            "repository_match": "current-repo",
            "ownership": "frontend-owned",
            "confirmation_required": False,
            "readiness": "manual-review-first",
            "effort": "easy",
            "risk": "low",
            "evidence_complete": True,
        }

        status, blockers = runner.repair_gate(config, item, approved=True)

        self.assertEqual(status, "done")
        self.assertEqual(blockers, [])

    def test_frontend_reverse_of_backend_list_order_is_redirected(self) -> None:
        issue = {
            "id": "3",
            "number": "BUG-3",
            "title": "前端把列表接口数据从正序改为倒序",
            "description": "接口返回顺序不符合预期，要求页面收到数据后 reverse。",
            "evidence_fetch": complete_evidence(),
        }

        result = runner.classify_issue(self.config(), issue, self.MATCH)
        status, blockers = runner.repair_gate(self.config(), result | self.MATCH, approved=True)

        self.assertEqual(result["ownership"], "backend-owned")
        self.assertEqual(result["readiness"], "redirect-to-owner")
        self.assertIn("API 契约", result["reason"])
        self.assertEqual(status, "blocked")
        self.assertTrue(blockers)

    def test_incomplete_comments_or_media_block_high_confidence_triage(self) -> None:
        issue = {
            "id": "4",
            "number": "BUG-4",
            "title": "按钮颜色与设计稿不一致",
            "description": "看起来只是 CSS 颜色。",
            "attachments": [
                {
                    "id": "file-1",
                    "name": "actual.png",
                    "media_kind": "image",
                    "inspection_state": "unknown",
                }
            ],
            "evidence_fetch": {
                "status": "partial",
                "detail": "complete",
                "comments": "error",
                "activities": "complete",
                "media": "partial",
                "findings": [],
                "missing": ["Comment access denied."],
            },
        }

        result = runner.classify_issue(self.config(), issue, self.MATCH)

        self.assertEqual(result["readiness"], "ask-for-confirmation")
        self.assertEqual(result["effort"], "blocked")
        self.assertFalse(result["evidence_complete"])
        self.assertRegex(" ".join(result["missing_information"]).lower(), r"comment|attachment|media")

    def test_inbound_comment_content_participates_in_ownership_triage(self) -> None:
        issue = {
            "id": "5",
            "number": "BUG-5",
            "title": "列表显示顺序需要调整",
            "description": "请结合最新评论判断。",
            "comments": [
                {
                    "id": "comment-1",
                    "content_text": "列表接口目前正序返回，工单却要求前端 reverse 成倒序。",
                    "attachments": [],
                }
            ],
            "evidence_fetch": complete_evidence("Latest comment clarifies that the API response order is the disputed behavior."),
        }

        result = runner.classify_issue(self.config(), issue, self.MATCH)

        self.assertEqual(result["ownership"], "backend-owned")
        self.assertEqual(result["readiness"], "redirect-to-owner")
        self.assertTrue(result["evidence_complete"])

    def test_inspected_attachment_summary_participates_in_triage(self) -> None:
        issue = {
            "id": "6",
            "number": "BUG-6",
            "title": "详情页显示异常",
            "description": "请查看录屏。",
            "attachments": [
                {
                    "id": "file-video",
                    "name": "repro.mp4",
                    "decision_relevant": True,
                    "inspection_state": "inspected",
                    "summary": "录屏 00:08 显示按钮被底部栏遮挡，属于当前详情页布局问题。",
                }
            ],
            "evidence_fetch": complete_evidence("The inspected recording confirms a local layout defect."),
        }

        result = runner.classify_issue(self.config(), issue, self.MATCH)

        self.assertEqual(result["ownership"], "frontend-owned")
        self.assertEqual(result["readiness"], "auto-fix-candidate")
        self.assertIn("遮挡", result["reason"])
        self.assertTrue(result["evidence_complete"])

    def test_plan_approval_cannot_bypass_incomplete_inbound_evidence(self) -> None:
        item = {
            "repository_match": "current-repo",
            "ownership": "frontend-owned",
            "confirmation_required": False,
            "readiness": "manual-review-first",
            "effort": "easy",
            "risk": "low",
            "evidence_complete": False,
        }

        status, blockers = runner.repair_gate(self.config(), item, approved=True)

        self.assertEqual(status, "blocked")
        self.assertRegex(" ".join(blockers).lower(), r"evidence|comment|attachment")

    def test_aggregate_complete_cannot_hide_an_uninspected_attachment(self) -> None:
        issue = {
            "attachments": [
                {
                    "id": "file-2",
                    "name": "repro.mp4",
                    "decision_relevant": True,
                    "inspection_state": "unknown",
                }
            ],
            "evidence_fetch": complete_evidence(),
        }

        result = runner.issue_evidence_state(issue)

        self.assertFalse(result["complete"])
        self.assertEqual(result["status"], "partial")
        self.assertRegex(" ".join(result["missing"]), r"repro\.mp4.*unknown")

    def test_new_comment_or_media_finding_changes_plan_fingerprint(self) -> None:
        issue = {
            "id": "7",
            "number": "BUG-7",
            "title": "详情页错位",
            "comments": [{"id": "comment-1", "content_text": "首次复现。", "attachments": []}],
            "evidence_fetch": complete_evidence("Screenshot shows a narrow-screen overflow."),
        }
        item = {
            "repository_match": "current-repo",
            "ownership": "frontend-owned",
            "readiness": "manual-review-first",
            "effort": "easy",
            "risk": "low",
        }
        args = argparse.Namespace(
            files=["src/pages/detail.tsx"],
            route="/detail/7",
            notes="",
            verification_mode=runner.STANDARD_VERIFICATION_MODE,
            completion_action=["commit"],
        )

        original = runner.fix_plan_fingerprint(issue, item, args)
        comment_changed = copy.deepcopy(issue)
        comment_changed["comments"][0]["content_text"] = "最新评论：仅在重新打开后复现。"
        media_changed = copy.deepcopy(issue)
        media_changed["evidence_fetch"]["findings"] = ["Video at 00:08 shows the first frame hidden."]

        self.assertNotEqual(original, runner.fix_plan_fingerprint(comment_changed, item, args))
        self.assertNotEqual(original, runner.fix_plan_fingerprint(media_changed, item, args))

    def test_hard_blocked_plan_is_a_diagnostic_without_fingerprint_or_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "issues"
            target = runner.write_issue_json(
                root,
                {
                    "id": "8",
                    "number": "BUG-8",
                    "title": "详情页偶发错位",
                    "evidence_fetch": {
                        "status": "partial",
                        "detail": "complete",
                        "comments": "partial",
                        "activities": "complete",
                        "media": "partial",
                    },
                },
            )
            item = {
                "repository_match": "current-repo",
                "ownership": "frontend-owned",
                "confirmation_required": False,
                "readiness": "ask-for-confirmation",
                "effort": "blocked",
                "risk": "medium",
                "evidence_complete": False,
            }
            args = argparse.Namespace(
                config="unused",
                local_config="",
                root=str(root),
                issue="BUG-8",
                approved="forged-fingerprint",
                files=["src/pages/detail.tsx"],
                route="/detail/8",
                notes="",
                verification_mode=runner.STANDARD_VERIFICATION_MODE,
                completion_action=["commit", "resolve-for-acceptance"],
            )

            output = io.StringIO()
            with mock.patch.object(
                runner, "load_config", return_value={"bugflow": {"root": str(root)}}
            ), mock.patch.object(runner, "triage_issue_dir", return_value=item), contextlib.redirect_stdout(output):
                result = runner.plan_fix(args)

            payload = json.loads(output.getvalue())
            artifact = (target / "fix-plan.md").read_text(encoding="utf-8")
            metadata = runner.frontmatter_metadata(target / "fix-plan.md")
            self.assertNotEqual(result, 0)
            self.assertTrue(payload["planning_diagnostic"])
            self.assertEqual(payload["plan_fingerprint"], "")
            self.assertEqual(payload["verification_mode"], "")
            self.assertEqual(payload["completion_actions"], [])
            self.assertEqual(metadata["planned_files"], "[]")
            self.assertEqual(metadata["verification_mode"], "")
            self.assertEqual(metadata["completion_actions"], "[]")
            self.assertIn("# 规划阻塞诊断", artifact)
            self.assertIn("可批准计划指纹: 未生成", artifact)
            self.assertNotIn("This exact fix plan has not been approved", artifact)
            self.assertNotIn("## 实施步骤", artifact)
            self.assertNotIn("## 远程工单策略", artifact)


class AssigneeFilterTests(unittest.TestCase):
    def test_exported_json_defaults_to_current_assignee_only(self) -> None:
        config = {"query_policy": {"assigned_to": "zhanghang", "assignee_aliases": ["user-7"]}}
        args = argparse.Namespace(assignee=None, include_all_assignees=False)
        tokens, mode = runner.import_assignee_filter(config, args, "jira")
        included, summary = runner.filter_imported_issues(
            [
                {"number": "BUG-1", "assignee": "ZhangHang"},
                {"number": "BUG-2", "assignee": ["someone-else"]},
                {"number": "BUG-3", "assignee": ["user-7"]},
            ],
            tokens,
            mode,
        )

        self.assertEqual([item["number"] for item in included], ["BUG-1", "BUG-3"])
        self.assertEqual(summary["skipped_assignee_count"], 1)

    def test_exported_json_requires_resolvable_current_assignee(self) -> None:
        config = {"query_policy": {"assigned_to": runner.CURRENT_LOGIN_USER}}
        args = argparse.Namespace(assignee=None, include_all_assignees=False)

        with self.assertRaises(SystemExit):
            runner.import_assignee_filter(config, args, "jira")

    def test_all_assignees_requires_explicit_opt_out(self) -> None:
        config = {"query_policy": {"assigned_to": ""}}
        args = argparse.Namespace(assignee=None, include_all_assignees=True)

        tokens, mode = runner.import_assignee_filter(config, args, "jira")

        self.assertEqual(tokens, set())
        self.assertEqual(mode, "explicit-all-assignees")


class VerificationTests(unittest.TestCase):
    @staticmethod
    def args(
        *,
        commands: list[str] | None = None,
        browser: str = "not-required",
        failed: bool = False,
        blocked: str = "",
        mode: str = runner.STANDARD_VERIFICATION_MODE,
        confidence: str = "",
        exemption_reason: str = "",
        evidence: list[str] | None = None,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            command=commands,
            browser=browser,
            browser_note="",
            evidence=evidence,
            residual_risk="无",
            failed=failed,
            blocked=blocked,
            mode=mode,
            confidence=confidence,
            exemption_reason=exemption_reason,
        )

    def test_empty_verification_is_not_done(self) -> None:
        self.assertNotEqual(runner.verification_status(self.args()), "done")

    def test_skipped_browser_is_not_done_even_with_a_passed_command(self) -> None:
        args = self.args(commands=["python -m unittest => passed"], browser="skipped")
        self.assertNotEqual(runner.verification_status(args), "done")

    def test_skipped_command_is_not_done(self) -> None:
        args = self.args(commands=["pnpm test => skipped"])
        self.assertNotEqual(runner.verification_status(args), "done")

    def test_structured_passed_command_can_complete_non_browser_verification(self) -> None:
        args = self.args(commands=["python -m unittest => passed"])
        self.assertEqual(runner.verification_status(args), "done")

    def test_lightweight_verification_can_finish_with_inspection_evidence(self) -> None:
        args = self.args(
            commands=["browser workflow => skipped: no deterministic fixture"],
            browser="skipped",
            mode=runner.LIGHTWEIGHT_VERIFICATION_MODE,
            confidence="high",
            exemption_reason="The external callback cannot be reproduced locally.",
            evidence=["Reviewed the exact callback guard and diff against the API contract."],
        )

        self.assertEqual(runner.verification_status(args), "done")

    def test_lightweight_verification_requires_reason_and_evidence(self) -> None:
        missing_reason = self.args(
            mode=runner.LIGHTWEIGHT_VERIFICATION_MODE,
            confidence="high",
            evidence=["Reviewed diff"],
        )
        missing_evidence = self.args(
            mode=runner.LIGHTWEIGHT_VERIFICATION_MODE,
            confidence="high",
            exemption_reason="No deterministic fixture",
        )

        self.assertNotEqual(runner.verification_status(missing_reason), "done")
        self.assertNotEqual(runner.verification_status(missing_evidence), "done")


class ArtifactSafetyTests(unittest.TestCase):
    def test_canonical_issue_id_keeps_readable_directory_name(self) -> None:
        self.assertEqual(artifacts.issue_dir(Path("issues"), "BUG-123").name, "BUG-123")

    def test_issue_dot_and_dotdot_are_rejected(self) -> None:
        root = Path("issues")
        for issue in (".", "..", "  .  ", "  ..  "):
            with self.subTest(issue=issue):
                with self.assertRaises((SystemExit, ValueError)):
                    artifacts.issue_dir(root, issue)

    def test_windows_reserved_issue_names_are_rejected(self) -> None:
        root = Path("issues")
        for issue in ("CON", "nul.txt", "PRN", "AUX.json", "COM1", "LPT9.log"):
            with self.subTest(issue=issue):
                with self.assertRaises((SystemExit, ValueError)):
                    artifacts.issue_dir(root, issue)

    def test_sanitized_issue_names_do_not_collide(self) -> None:
        root = Path("issues")
        slash = artifacts.issue_dir(root, "BUG/123")
        dash = artifacts.issue_dir(root, "BUG-123")
        backslash = artifacts.issue_dir(root, r"BUG\123")

        self.assertEqual(len({slash.name, dash.name, backslash.name}), 3)
        for target in (slash, dash, backslash):
            self.assertTrue(target.resolve().is_relative_to(root.resolve()))

    def test_unchanged_issue_keeps_downstream_but_changed_issue_invalidates_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "issues"
            issue = {
                "source": "test",
                "id": "123",
                "number": "BUG-123",
                "title": "Original title",
                "description": "Original description",
                "requirements": [],
            }
            target = runner.write_issue_json(root, issue)
            downstream = [item for item in runner.ARTIFACTS if item["file"] != "issue.json"]
            for item in downstream:
                runner.write_markdown_artifact(
                    target / item["file"], item["id"], "done", f"# {item['id']}\n"
                )

            runner.write_issue_json(root, copy.deepcopy(issue))
            self.assertTrue(
                all(
                    runner.artifact_frontmatter_status(target / item["file"]) == "done"
                    for item in downstream
                ),
                "an unchanged refresh must not discard completed work",
            )

            changed = {**issue, "title": "Changed upstream title"}
            runner.write_issue_json(root, changed)

            for item in downstream:
                with self.subTest(artifact=item["id"]):
                    self.assertNotEqual(
                        runner.artifact_frontmatter_status(target / item["file"]),
                        "done",
                        f"{item['id']} stayed done after issue intake changed",
                    )


class InitProjectTests(unittest.TestCase):
    def assert_yaml_scalar(self, text: str, key: str, expected: str) -> None:
        pattern = rf"(?m)^\s*{re.escape(key)}:\s*[\"']?{re.escape(expected)}[\"']?\s*$"
        self.assertRegex(text, pattern)

    def test_init_project_honors_platform_root_schema_and_ignores_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            config_path = Path(".codex/bugflow/custom.project.yaml")
            local_path = Path(".codex/bugflow/custom.local.yaml")
            schema_path = Path(".codex/bugflow/custom-schema.yaml")
            artifact_root = ".private-bugflow/issues"
            parser = runner.build_parser()
            args = parser.parse_args(
                [
                    "--config",
                    config_path.as_posix(),
                    "--local-config",
                    local_path.as_posix(),
                    "--root",
                    artifact_root,
                    "init-project",
                    "--platform",
                    "jira",
                    "--project-name",
                    "safety-tests",
                    "--project-key",
                    "SAFE",
                    "--schema",
                    schema_path.as_posix(),
                ]
            )

            with working_directory(repo), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(args.func(args), 0)
                self.assertEqual(args.func(args), 0)

            project_text = (repo / config_path).read_text(encoding="utf-8")
            local_text = (repo / local_path).read_text(encoding="utf-8")
            local_config = runner.load_yaml(repo / local_path)
            schema_text = (repo / schema_path).read_text(encoding="utf-8")
            gitignore_lines = {
                line.strip().replace("\\", "/")
                for line in (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
                if line.strip()
            }

            self.assert_yaml_scalar(project_text, "platform", "jira")
            self.assert_yaml_scalar(project_text, "project_key", "SAFE")
            self.assert_yaml_scalar(project_text, "root", artifact_root)
            self.assert_yaml_scalar(project_text, "schema", schema_path.as_posix())
            self.assert_yaml_scalar(schema_text, "root", artifact_root)
            self.assertIn(config_path.as_posix(), local_text)
            self.assertNotIn("remote_status_policy", local_config)
            self.assertNotIn("execution_policy", local_config)
            self.assertNotIn("git_policy", local_config)
            self.assertEqual(local_config["query_policy"]["assigned_to"], "")
            self.assertIn(".private-bugflow/", gitignore_lines)
            self.assertIn(local_path.as_posix(), gitignore_lines)
            self.assertEqual(
                (repo / ".gitignore")
                .read_text(encoding="utf-8")
                .replace("\\", "/")
                .count(".private-bugflow/"),
                1,
            )
            self.assertEqual(
                (repo / ".gitignore")
                .read_text(encoding="utf-8")
                .replace("\\", "/")
                .count(local_path.as_posix()),
                1,
            )


class DoctorTests(unittest.TestCase):
    def test_exported_json_platform_does_not_require_project_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            config_path = repo / ".codex/bugflow/issue-triage.project.yaml"
            schema_path = repo / ".codex/bugflow/schema.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("jira export config\n", encoding="utf-8")
            schema_path.write_text("schema\n", encoding="utf-8")
            (repo / ".gitignore").write_text(".bugflow/\n", encoding="utf-8")
            config = {
                "issue_source": {
                    "platform": "jira",
                    "project_key": "",
                    "work_item_type": "issue",
                },
                "field_mapping": {
                    "id": "id",
                    "number": "number",
                    "title": "title",
                    "status": "status",
                    "requirements": "requirements",
                },
                "query_policy": {"assigned_to": "current-user"},
                "bugflow": {
                    "enabled": True,
                    "root": ".bugflow/issues",
                    "schema": str(schema_path),
                    "commit_artifacts_by_default": False,
                },
            }
            args = argparse.Namespace(
                config=str(config_path),
                local_config="",
                root=".bugflow/issues",
                json=True,
            )
            output = io.StringIO()
            with working_directory(repo), mock.patch.object(
                runner, "load_config", return_value=config
            ), contextlib.redirect_stdout(output):
                exit_code = runner.doctor(args)

            checks = json.loads(output.getvalue())["checks"]
            issue_source_checks = [
                item for item in checks if item["item"] == "issue-source"
            ]
            self.assertEqual(exit_code, 0)
            self.assertTrue(
                any(item["level"] == "ok" for item in issue_source_checks),
                "exported JSON platforms should not require a Feishu-style project key",
            )

    def test_starter_values_and_unverified_status_ids_are_not_reported_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            config_path = repo / ".codex/bugflow/issue-triage.project.yaml"
            schema_path = repo / ".codex/bugflow/schema.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("starter config\n", encoding="utf-8")
            schema_path.write_text("starter schema\n", encoding="utf-8")
            (repo / ".gitignore").write_text(".bugflow/\n", encoding="utf-8")
            config = {
                "issue_source": {
                    "platform": "feishu-project",
                    "project_key": "your-project-key",
                    "work_item_type": "issue",
                },
                "field_mapping": {
                    "id": "id",
                    "number": "number",
                    "title": "title",
                    "status": "status",
                    "requirements": "requirements",
                },
                "statuses": {
                    "open": {"id": "OPEN", "label": "待修复"},
                    "in_progress": {"id": "", "label": "修复中"},
                },
                "query_policy": {"assigned_to": runner.CURRENT_LOGIN_USER},
                "bugflow": {
                    "enabled": True,
                    "root": ".bugflow/issues",
                    "schema": str(schema_path),
                    "commit_artifacts_by_default": False,
                },
            }
            args = argparse.Namespace(
                config=str(config_path),
                local_config="",
                root=".bugflow/issues",
                json=True,
            )
            output = io.StringIO()
            with working_directory(repo), mock.patch.object(
                runner, "load_config", return_value=config
            ), contextlib.redirect_stdout(output):
                runner.doctor(args)

            checks = json.loads(output.getvalue())["checks"]
            issue_source_checks = [
                item for item in checks if item["item"] == "issue-source"
            ]
            status_checks = [item for item in checks if item["item"] == "status-codes"]
            self.assertTrue(issue_source_checks)
            self.assertTrue(status_checks)
            self.assertFalse(
                any(item["level"] == "ok" for item in issue_source_checks),
                "starter project_key was reported as configured",
            )
            self.assertFalse(
                any(item["level"] == "ok" for item in status_checks),
                "placeholder or incomplete status ids were reported as verified",
            )


class MqlSafetyTests(unittest.TestCase):
    @staticmethod
    def config() -> dict[str, object]:
        return {
            "issue_source": {
                "platform": "feishu-project",
                "project_key": "ai-rays",
                "work_item_type": "issue",
                "default_status": "OPEN",
            },
            "field_mapping": {
                "id": "work_item_id",
                "number": "auto_number",
                "title": "name",
                "status": "work_item_status",
                "assignee": "current_status_operator",
                "updated_at": "updated_at",
            },
            "query_policy": {
                "assigned_to": "current_login_user()",
                "status": "OPEN",
                "limit": 20,
                "order_by": "updated_at desc",
            },
        }

    def test_valid_identifiers_and_order_build_expected_mql(self) -> None:
        result = runner.build_feishu_mql(self.config())

        self.assertIn("FROM `ai-rays`.`issue`", result["mql"])
        self.assertIn("ORDER BY `updated_at` DESC", result["mql"])
        self.assertIn("LIMIT 20", result["mql"])

    def test_project_key_with_backtick_is_rejected(self) -> None:
        config = self.config()
        config["issue_source"]["project_key"] = "ai-rays` UNION SELECT"

        with self.assertRaises(SystemExit):
            runner.build_feishu_mql(config)

    def test_order_by_injection_is_rejected(self) -> None:
        config = self.config()
        config["query_policy"]["order_by"] = "updated_at desc; DROP TABLE issues"

        with self.assertRaises(SystemExit):
            runner.build_feishu_mql(config)

    def test_limit_must_be_between_one_and_one_hundred(self) -> None:
        for limit in (0, 101):
            config = self.config()
            config["query_policy"]["limit"] = limit
            with self.subTest(limit=limit):
                with self.assertRaises(SystemExit):
                    runner.build_feishu_mql(config)

    def test_string_literals_escape_single_quotes(self) -> None:
        config = self.config()
        config["query_policy"].update(
            {"assigned_to": "user'o", "status": "待'修复"}
        )

        mql = runner.build_feishu_mql(config)["mql"]

        self.assertIn("'user''o'", mql)
        self.assertIn("'待''修复'", mql)


class WorkflowDependencyTests(unittest.TestCase):
    @staticmethod
    def issue() -> dict[str, object]:
        return {
            "source": "test",
            "id": "7",
            "number": "BUG-7",
            "title": "Dependency gate",
            "requirements": [],
            "evidence_fetch": complete_evidence(),
        }

    @staticmethod
    def config(root: Path) -> dict[str, object]:
        return {"bugflow": {"root": str(root)}}

    def test_record_implementation_requires_completed_fix_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "issues"
            target = runner.write_issue_json(root, self.issue())
            args = argparse.Namespace(
                config="unused",
                local_config="",
                root=str(root),
                issue="BUG-7",
                summary=["Should not be accepted"],
                files=["src/file.py"],
                remote_status="未修改",
                commit="",
                notes="",
                blocked="",
            )

            with mock.patch.object(runner, "load_config", return_value=self.config(root)):
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = runner.record_implementation(args)
                except SystemExit:
                    pass
                else:
                    self.assertNotEqual(result, 0)

            self.assertNotEqual(
                runner.artifact_frontmatter_status(target / "implementation.md"), "done"
            )

    def test_record_verification_requires_completed_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "issues"
            target = runner.write_issue_json(root, self.issue())
            for artifact_id in ("requirement-match", "triage-report", "fix-plan"):
                runner.write_markdown_artifact(
                    runner.artifact_path(target, artifact_id),
                    artifact_id,
                    "done",
                    f"# {artifact_id}\n",
                )
            args = argparse.Namespace(
                config="unused",
                local_config="",
                root=str(root),
                issue="BUG-7",
                command=["python -m unittest => passed"],
                browser="not-required",
                browser_note="",
                evidence=None,
                residual_risk="无",
                failed=False,
                blocked="",
            )

            with mock.patch.object(runner, "load_config", return_value=self.config(root)):
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = runner.record_verification(args)
                except SystemExit:
                    pass
                else:
                    self.assertNotEqual(result, 0)

            self.assertNotEqual(
                runner.artifact_frontmatter_status(target / "verification.md"), "done"
            )

    def test_record_implementation_rejects_done_but_unapproved_fix_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree = Path(temp_dir)
            root = worktree / "issues"
            target = runner.write_issue_json(root, self.issue())
            (worktree / "src").mkdir()
            (worktree / "src/file.py").write_text("print('change')\n", encoding="utf-8")
            for artifact_id in ("requirement-match", "triage-report"):
                runner.write_markdown_artifact(
                    runner.artifact_path(target, artifact_id),
                    artifact_id,
                    "done",
                    f"# {artifact_id}\n",
                )
            runner.write_markdown_artifact(
                runner.artifact_path(target, "fix-plan"),
                "fix-plan",
                "done",
                "# Unapproved plan\n",
                {"plan_fingerprint": "unapproved-plan", "fix_approved": False},
            )
            args = argparse.Namespace(
                config="unused",
                local_config="",
                root=str(root),
                issue="BUG-7",
                summary=["Should not be accepted"],
                files=["src/file.py"],
                remote_status="未修改",
                commit="",
                notes="",
                blocked="",
            )

            with working_directory(worktree), mock.patch.object(
                runner, "load_config", return_value=self.config(root)
            ):
                with self.assertRaises(SystemExit) as raised:
                    with contextlib.redirect_stdout(io.StringIO()):
                        runner.record_implementation(args)

            self.assertRegex(str(raised.exception).lower(), r"approval|approved")
            self.assertNotEqual(
                runner.artifact_frontmatter_status(target / "implementation.md"), "done"
            )

    def test_record_implementation_must_match_approved_plan_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree = Path(temp_dir)
            root = worktree / "issues"
            target = runner.write_issue_json(root, self.issue())
            (worktree / "src").mkdir()
            (worktree / "src/planned.py").write_text("print('planned')\n", encoding="utf-8")
            (worktree / "src/other.py").write_text("print('other')\n", encoding="utf-8")
            for artifact_id in ("requirement-match", "triage-report"):
                runner.write_markdown_artifact(
                    runner.artifact_path(target, artifact_id),
                    artifact_id,
                    "done",
                    f"# {artifact_id}\n",
                )
            runner.write_markdown_artifact(
                runner.artifact_path(target, "fix-plan"),
                "fix-plan",
                "done",
                "# Approved plan\n",
                {
                    "plan_fingerprint": "approved-plan",
                    "fix_approved": True,
                    "planned_files": ["src/planned.py"],
                },
            )
            args = argparse.Namespace(
                config="unused",
                local_config="",
                root=str(root),
                issue="BUG-7",
                summary=["Changed a different file"],
                files=["src/other.py"],
                remote_status="未修改",
                commit="",
                notes="",
                blocked="",
            )

            with working_directory(worktree), mock.patch.object(
                runner, "load_config", return_value=self.config(root)
            ):
                with self.assertRaises(SystemExit) as raised:
                    with contextlib.redirect_stdout(io.StringIO()):
                        runner.record_implementation(args)

            self.assertRegex(str(raised.exception).lower(), r"plan|planned|files")
            self.assertNotEqual(
                runner.artifact_frontmatter_status(target / "implementation.md"), "done"
            )

    def test_plan_approved_lightweight_verification_records_done(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "issues"
            target = runner.write_issue_json(root, self.issue())
            runner.write_markdown_artifact(
                runner.artifact_path(target, "requirement-match"),
                "requirement-match",
                "done",
                "# matched\n",
            )
            runner.write_markdown_artifact(
                runner.artifact_path(target, "triage-report"),
                "triage-report",
                "done",
                "# triage\n",
                {
                    "repository_match": "current-repo",
                    "confidence": "high",
                    "confirmation_required": False,
                    "ownership": "frontend-owned",
                    "effort": "medium",
                    "readiness": "manual-review-first",
                    "risk": "medium",
                    "evidence_complete": True,
                },
            )
            runner.write_markdown_artifact(
                runner.artifact_path(target, "fix-plan"),
                "fix-plan",
                "done",
                "# approved lightweight plan\n",
                {
                    "plan_fingerprint": "light-plan",
                    "fix_approved": True,
                    "verification_mode": runner.LIGHTWEIGHT_VERIFICATION_MODE,
                    "completion_actions": ["commit"],
                },
            )
            runner.write_markdown_artifact(
                runner.artifact_path(target, "implementation"),
                "implementation",
                "done",
                "# implementation\n",
                {"summary_count": 1, "file_count": 1, "files": ["src/file.py"]},
            )
            args = argparse.Namespace(
                config="unused",
                local_config="",
                root=str(root),
                issue="BUG-7",
                mode=runner.LIGHTWEIGHT_VERIFICATION_MODE,
                confidence="high",
                exemption_reason="The upstream callback has no deterministic local fixture.",
                command=["end-to-end callback => skipped: external dependency"],
                browser="skipped",
                browser_note="External callback cannot be reproduced locally.",
                evidence=["Reviewed the scoped diff and callback contract."],
                residual_risk="Requires acceptance testing with the real callback.",
                failed=False,
                blocked="",
            )
            config = {
                "bugflow": {"root": str(root)},
                "execution_policy": {"allow_lightweight_verification": True},
            }

            with mock.patch.object(runner, "load_config", return_value=config), contextlib.redirect_stdout(
                io.StringIO()
            ):
                result = runner.record_verification(args)

            metadata = runner.frontmatter_metadata(target / "verification.md")
            self.assertEqual(result, 0)
            self.assertEqual(metadata["verification_mode"], runner.LIGHTWEIGHT_VERIFICATION_MODE)
            self.assertEqual(metadata["lightweight_approved"], "true")


class GitCommitSafetyTests(unittest.TestCase):
    def git(self, repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and result.returncode != 0:
            self.fail(result.stderr or result.stdout or f"git {' '.join(args)} failed")
        return result

    def create_repo(self, repo: Path) -> str:
        self.git(repo, "init", "--quiet")
        self.git(repo, "config", "user.name", "Bugflow Safety Tests")
        self.git(repo, "config", "user.email", "bugflow-tests@example.invalid")
        (repo / "target.txt").write_text("base target\n", encoding="utf-8")
        (repo / "unrelated.txt").write_text("base unrelated\n", encoding="utf-8")
        self.git(repo, "add", "--", "target.txt", "unrelated.txt")
        self.git(repo, "commit", "--quiet", "-m", "baseline")
        return self.git(repo, "rev-parse", "HEAD").stdout.strip()

    @staticmethod
    def workflow_config(root: Path) -> dict[str, object]:
        return {
            "project": {"role_assumption": "frontend", "repo_path": "."},
            "bugflow": {"root": str(root)},
            "requirement_mapping": {
                "enabled": True,
                "current_repo": {
                    "repo_key": "current-repo",
                    "path": ".",
                    "aliases": ["web client"],
                },
                "related_repositories": [],
                "demand_rules": [],
            },
            "execution_policy": {
                "auto_fix_allowed": True,
                "auto_fix_low_risk_frontend": True,
                "allow_lightweight_verification": True,
                "approved_completion_actions": ["commit"],
                "max_auto_fix_effort": "medium",
            },
            "verification": {"test": "python -m unittest"},
            "browser_verification": {"enabled": False},
            "git_policy": {
                "auto_commit_after_fix": True,
                "commit_after_verification_only": True,
                "stage_policy": "touched-files-only",
                "push_after_commit": False,
                "commit_message_template": "fix({issue}): {title}",
            },
        }

    @staticmethod
    def workflow_issue() -> dict[str, object]:
        return {
            "source": "test",
            "id": "1",
            "number": "BUG-1",
            "title": "列表按钮颜色偏差",
            "description": "只修改 web client 的按钮 CSS 颜色。",
            "status": "OPEN",
            "requirements": [{"id": "REQ-1", "title": "web client"}],
            "evidence_fetch": complete_evidence(),
        }

    @staticmethod
    def commit_args(
        root: Path, *, authorized: str | None = None
    ) -> argparse.Namespace:
        if authorized is None:
            target = runner.issue_dir(root, "BUG-1")
            authorized = runner.frontmatter_metadata(
                runner.artifact_path(target, "fix-plan")
            ).get("plan_fingerprint", "")
        return argparse.Namespace(
            config="unused",
            local_config="",
            root=str(root),
            issue="BUG-1",
            files=["target.txt"],
            message="",
            dry_run=False,
            authorized=authorized,
        )

    def prepare_verified_issue(
        self,
        root: Path,
        config: dict[str, object],
        *,
        lightweight: bool = False,
    ) -> dict[str, object]:
        issue = self.workflow_issue()
        runner.write_issue_json(root, issue)
        common = {"config": "unused", "local_config": "", "root": str(root), "issue": "BUG-1"}
        plan_args = argparse.Namespace(
            **common,
            approved="",
            files=["target.txt"],
            route="",
            notes="",
            verification_mode=(
                runner.LIGHTWEIGHT_VERIFICATION_MODE
                if lightweight
                else runner.STANDARD_VERIFICATION_MODE
            ),
            completion_action=None,
        )
        implementation_args = argparse.Namespace(
            **common,
            summary=["Update target file"],
            files=["target.txt"],
            remote_status="未修改",
            commit="",
            notes="",
            blocked="",
        )
        verification_args = argparse.Namespace(
            **common,
            mode=(
                runner.LIGHTWEIGHT_VERIFICATION_MODE
                if lightweight
                else runner.STANDARD_VERIFICATION_MODE
            ),
            confidence="high" if lightweight else "",
            exemption_reason=(
                "The external callback has no deterministic local fixture."
                if lightweight
                else ""
            ),
            command=(
                ["external callback => skipped: no local fixture"]
                if lightweight
                else ["python -m unittest => passed"]
            ),
            browser="skipped" if lightweight else "not-required",
            browser_note="External callback unavailable" if lightweight else "",
            evidence=["Reviewed the exact scoped diff and callback contract."] if lightweight else None,
            residual_risk="Acceptance test with real callback" if lightweight else "无",
            failed=False,
            blocked="",
        )
        with mock.patch.object(runner, "load_config", return_value=config):
            first_plan_output = io.StringIO()
            with contextlib.redirect_stdout(first_plan_output):
                self.assertNotEqual(runner.plan_fix(plan_args), 0)
            plan_result = json.loads(first_plan_output.getvalue())
            fingerprint = plan_result["plan_fingerprint"]
            self.assertTrue(fingerprint)
            self.assertIs(plan_result["approved"], False)

            plan_args.approved = fingerprint
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.plan_fix(plan_args), 0)
                self.assertEqual(runner.record_implementation(implementation_args), 0)
                self.assertEqual(runner.record_verification(verification_args), 0)
        return issue

    def test_plan_fix_rejects_boolean_true_instead_of_exact_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            with working_directory(repo):
                runner.write_issue_json(root, self.workflow_issue())
                args = argparse.Namespace(
                    config="unused",
                    local_config="",
                    root=str(root),
                    issue="BUG-1",
                    approved=True,
                    files=["target.txt"],
                    route="",
                    notes="",
                )
                output = io.StringIO()
                with mock.patch.object(
                    runner, "load_config", return_value=config
                ), contextlib.redirect_stdout(output):
                    result = runner.plan_fix(args)

                plan_result = json.loads(output.getvalue())
                self.assertNotEqual(result, 0)
                self.assertIs(plan_result["approved"], False)
                self.assertTrue(plan_result["plan_fingerprint"])
                self.assertRegex(
                    " ".join(plan_result["blockers"]).lower(), r"fingerprint|approved"
                )

    def test_commit_fix_never_includes_pre_staged_unrelated_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            baseline = self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            with working_directory(repo):
                self.prepare_verified_issue(root, config)
                (repo / "target.txt").write_text("fixed target\n", encoding="utf-8")
                (repo / "unrelated.txt").write_text("user staged work\n", encoding="utf-8")
                self.git(repo, "add", "--", "unrelated.txt")
                args = self.commit_args(root)

                rejected = False
                rejection = ""
                with mock.patch.object(runner, "load_config", return_value=config):
                    try:
                        with contextlib.redirect_stdout(io.StringIO()):
                            result = runner.commit_fix(args)
                    except SystemExit as exc:
                        rejected = True
                        rejection = str(exc)
                    else:
                        self.assertEqual(result, 0)

                if rejected:
                    self.assertRegex(rejection.lower(), r"pre.?staged|staged|index")
                    self.assertEqual(self.git(repo, "rev-parse", "HEAD").stdout.strip(), baseline)
                else:
                    committed = {
                        line.strip()
                        for line in self.git(
                            repo,
                            "diff-tree",
                            "--no-commit-id",
                            "--name-only",
                            "-r",
                            "HEAD",
                        ).stdout.splitlines()
                        if line.strip()
                    }
                    self.assertEqual(committed, {"target.txt"})

                staged_after = {
                    line.strip()
                    for line in self.git(repo, "diff", "--cached", "--name-only").stdout.splitlines()
                    if line.strip()
                }
                self.assertIn("unrelated.txt", staged_after)

    def test_commit_fix_creates_real_single_file_commit_and_leaves_clean_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            baseline = self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            with working_directory(repo):
                self.prepare_verified_issue(root, config)
                (repo / "target.txt").write_text("verified fix\n", encoding="utf-8")

                output = io.StringIO()
                with mock.patch.object(
                    runner, "load_config", return_value=config
                ), contextlib.redirect_stdout(output):
                    self.assertEqual(runner.commit_fix(self.commit_args(root)), 0)

                commit_result = json.loads(output.getvalue())
                head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
                committed = {
                    line.strip()
                    for line in self.git(
                        repo,
                        "diff-tree",
                        "--no-commit-id",
                        "--name-only",
                        "-r",
                        "HEAD",
                    ).stdout.splitlines()
                    if line.strip()
                }
                self.assertNotEqual(head, baseline)
                self.assertEqual(committed, {"target.txt"})
                self.assertEqual(
                    self.git(repo, "diff", "--cached", "--name-only").stdout, ""
                )
                self.assertEqual(
                    self.git(repo, "status", "--porcelain", "--", "target.txt").stdout,
                    "",
                )
                self.assertIs(commit_result["pushed"], False)

    def test_commit_fix_accepts_plan_approved_lightweight_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            baseline = self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            with working_directory(repo):
                self.prepare_verified_issue(root, config, lightweight=True)
                (repo / "target.txt").write_text("lightweight verified fix\n", encoding="utf-8")

                with mock.patch.object(
                    runner, "load_config", return_value=config
                ), contextlib.redirect_stdout(io.StringIO()):
                    result = runner.commit_fix(self.commit_args(root))

                self.assertEqual(result, 0)
                self.assertNotEqual(self.git(repo, "rev-parse", "HEAD").stdout.strip(), baseline)

    def test_commit_fix_requires_plan_authorization_even_when_auto_commit_is_enabled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            baseline = self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            with working_directory(repo):
                self.prepare_verified_issue(root, config)
                (repo / "target.txt").write_text("unauthorized fix\n", encoding="utf-8")

                with mock.patch.object(runner, "load_config", return_value=config):
                    with self.assertRaises(SystemExit) as raised:
                        with contextlib.redirect_stdout(io.StringIO()):
                            runner.commit_fix(self.commit_args(root, authorized=""))

                self.assertRegex(
                    str(raised.exception).lower(), r"authorization|fingerprint|approval"
                )
                self.assertEqual(self.git(repo, "rev-parse", "HEAD").stdout.strip(), baseline)

    def test_commit_fix_requires_commit_in_approved_completion_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            baseline = self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            config["execution_policy"]["approved_completion_actions"] = []
            with working_directory(repo):
                self.prepare_verified_issue(root, config)
                (repo / "target.txt").write_text("unplanned commit\n", encoding="utf-8")

                with mock.patch.object(runner, "load_config", return_value=config):
                    with self.assertRaises(SystemExit) as raised:
                        with contextlib.redirect_stdout(io.StringIO()):
                            runner.commit_fix(self.commit_args(root))

                self.assertRegex(str(raised.exception).lower(), r"completion action|authorize.*commit")
                self.assertEqual(self.git(repo, "rev-parse", "HEAD").stdout.strip(), baseline)

    def test_plan_authorization_can_allow_commit_when_project_capability_is_disabled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            baseline = self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            config["git_policy"]["auto_commit_after_fix"] = False
            with working_directory(repo):
                self.prepare_verified_issue(root, config)
                (repo / "target.txt").write_text(
                    "explicitly authorized fix\n", encoding="utf-8"
                )

                with mock.patch.object(
                    runner, "load_config", return_value=config
                ), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.commit_fix(self.commit_args(root)), 0)

                self.assertNotEqual(
                    self.git(repo, "rev-parse", "HEAD").stdout.strip(), baseline
                )

    def test_local_commit_deny_cannot_be_bypassed_by_plan_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            baseline = self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            config["_bugflow_safety"] = {
                "local_denies": ["git_policy.auto_commit_after_fix"]
            }
            with working_directory(repo):
                self.prepare_verified_issue(root, config)
                (repo / "target.txt").write_text("locally denied fix\n", encoding="utf-8")

                with mock.patch.object(runner, "load_config", return_value=config):
                    with self.assertRaises(SystemExit) as raised:
                        with contextlib.redirect_stdout(io.StringIO()):
                            runner.commit_fix(self.commit_args(root))

                self.assertRegex(str(raised.exception).lower(), r"local deny|disables")
                self.assertEqual(self.git(repo, "rev-parse", "HEAD").stdout.strip(), baseline)

    def test_commit_fix_rejects_file_not_in_verified_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            baseline = self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            with working_directory(repo):
                self.prepare_verified_issue(root, config)
                (repo / "unrelated.txt").write_text(
                    "different file after verification\n", encoding="utf-8"
                )
                args = self.commit_args(root)
                args.files = ["unrelated.txt"]

                with mock.patch.object(runner, "load_config", return_value=config):
                    with self.assertRaises(SystemExit) as raised:
                        with contextlib.redirect_stdout(io.StringIO()):
                            runner.commit_fix(args)

                self.assertRegex(
                    str(raised.exception).lower(), r"exactly match|implementation|verified"
                )
                self.assertEqual(self.git(repo, "rev-parse", "HEAD").stdout.strip(), baseline)
                self.assertEqual(
                    self.git(repo, "diff", "--cached", "--name-only").stdout, ""
                )

    def test_failed_git_commit_clears_index_but_preserves_worktree_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            baseline = self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            with working_directory(repo):
                self.prepare_verified_issue(root, config)
                (repo / "target.txt").write_text("fix that hook rejects\n", encoding="utf-8")
                hook = repo / ".git/hooks/pre-commit"
                hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

                with mock.patch.object(runner, "load_config", return_value=config):
                    with self.assertRaises(SystemExit):
                        with contextlib.redirect_stdout(io.StringIO()):
                            runner.commit_fix(self.commit_args(root))

                self.assertEqual(self.git(repo, "rev-parse", "HEAD").stdout.strip(), baseline)
                self.assertEqual(
                    self.git(repo, "diff", "--cached", "--name-only").stdout, ""
                )
                worktree_changes = {
                    line.strip()
                    for line in self.git(repo, "diff", "--name-only").stdout.splitlines()
                    if line.strip()
                }
                self.assertEqual(worktree_changes, {"target.txt"})

    def test_commit_fix_rejects_forged_done_verification_with_pending_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            baseline = self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            with working_directory(repo):
                target = runner.write_issue_json(root, self.workflow_issue())
                runner.write_markdown_artifact(
                    runner.artifact_path(target, "verification"),
                    "verification",
                    "done",
                    "# Verification\n\n- python -m unittest => passed\n",
                )
                (repo / "target.txt").write_text("fixed target\n", encoding="utf-8")

                with mock.patch.object(runner, "load_config", return_value=config):
                    with self.assertRaises(SystemExit) as raised:
                        with contextlib.redirect_stdout(io.StringIO()):
                            runner.commit_fix(self.commit_args(root))

                self.assertRegex(
                    str(raised.exception).lower(),
                    r"upstream|implementation|fix.?plan|verification|stale",
                )
                self.assertEqual(self.git(repo, "rev-parse", "HEAD").stdout.strip(), baseline)

    def test_old_verification_cannot_commit_after_issue_or_implementation_changes(self) -> None:
        for changed_upstream in ("issue", "implementation"):
            with self.subTest(changed_upstream=changed_upstream):
                with tempfile.TemporaryDirectory() as temp_dir:
                    repo = Path(temp_dir)
                    baseline = self.create_repo(repo)
                    root = repo / ".bugflow/issues"
                    config = self.workflow_config(root)
                    with working_directory(repo):
                        issue = self.prepare_verified_issue(root, config)
                        target = runner.issue_dir(root, "BUG-1")
                        self.assertEqual(
                            runner.artifact_frontmatter_status(target / "verification.md"),
                            "done",
                        )

                        if changed_upstream == "issue":
                            runner.write_issue_json(
                                root,
                                {**issue, "title": "Changed after verification"},
                            )
                        else:
                            implementation_args = argparse.Namespace(
                                config="unused",
                                local_config="",
                                root=str(root),
                                issue="BUG-1",
                                summary=["Implementation changed after verification"],
                                files=["target.txt"],
                                remote_status="未修改",
                                commit="",
                                notes="",
                                blocked="",
                            )
                            with mock.patch.object(
                                runner, "load_config", return_value=config
                            ), contextlib.redirect_stdout(io.StringIO()):
                                self.assertEqual(
                                    runner.record_implementation(implementation_args), 0
                                )

                        self.assertNotEqual(
                            runner.artifact_frontmatter_status(target / "verification.md"),
                            "done",
                            "upstream change left old verification reusable",
                        )
                        (repo / "target.txt").write_text(
                            "fixed target after upstream change\n", encoding="utf-8"
                        )
                        with mock.patch.object(runner, "load_config", return_value=config):
                            with self.assertRaises(SystemExit) as raised:
                                with contextlib.redirect_stdout(io.StringIO()):
                                    runner.commit_fix(self.commit_args(root))

                        self.assertRegex(
                            str(raised.exception).lower(),
                            r"upstream|implementation|fix.?plan|verification|stale|pending",
                        )
                        self.assertEqual(
                            self.git(repo, "rev-parse", "HEAD").stdout.strip(), baseline
                        )

    def test_commit_file_validation_rejects_dot_directories_and_pathspec_magic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            (repo / "src").mkdir()
            (repo / "src/file.py").write_text("print('ok')\n", encoding="utf-8")
            with working_directory(repo):
                for invalid in (
                    ".",
                    "src",
                    "missing.py",
                    ":(glob)**/*.py",
                    "*.py",
                    "src/*.py",
                ):
                    with self.subTest(path=invalid):
                        with self.assertRaises((SystemExit, ValueError)):
                            runner.ensure_files_inside_cwd([invalid])

                runner.ensure_files_inside_cwd(["src/file.py"])


class RawPayloadRedactionTests(unittest.TestCase):
    @staticmethod
    def nested_sensitive_payload() -> dict[str, object]:
        return {
            "id": "BUG-NESTED-SECRET",
            "title": "Nested secret-bearing payload",
            "description": (
                "https://example.invalid/description?token=description-token"
                "&signature=description-signature"
            ),
            "source_url": (
                "https://example.invalid/source?X-Amz-Signature=source-signature"
            ),
            "requirements": [
                {
                    "id": "REQ-1",
                    "title": "Requirement",
                    "url": "https://example.invalid/requirement?token=requirement-url-token",
                    "metadata": {
                        "secret": "requirement-raw-secret",
                        "authorization": "Bearer requirement-bearer",
                    },
                }
            ],
            "attachments": [
                {
                    "name": "evidence.png",
                    "url": "https://example.invalid/file?signature=attachment-signature",
                    "access_token": "attachment-token",
                }
            ],
            "comments": [
                {
                    "comment_id": "comment-1",
                    "content": (
                        "See <a href=\"https://example.invalid/comment?sign=comment-sign"
                        "&token=comment-token\">evidence</a>"
                    ),
                    "X-Meego-File-Sign": "comment-header-sign",
                }
            ],
            "activities": [
                {
                    "record_id": "activity-1",
                    "summary": "Bearer activity-bearer",
                }
            ],
            "evidence_fetch": {
                "status": "partial",
                "detail": "complete",
                "comments": "complete",
                "activities": "complete",
                "media": "partial",
                "findings": ["https://example.invalid/media?sign=evidence-sign"],
                "missing": ["download token=evidence-token"],
            },
        }

    def test_raw_payload_is_redacted_by_default(self) -> None:
        payload = {
            "id": "BUG-SECRET",
            "title": "Secret-bearing payload",
            "access_token": "top-secret-token",
            "authorization": "Bearer top-secret-bearer",
            "nested": {
                "password": "top-secret-password",
                "note": "request used Bearer embedded-secret-bearer",
                "signed_url": (
                    "https://example.invalid/file?token=url-secret-token"
                    "&X-Amz-Signature=url-secret-signature&safe=visible"
                ),
            },
        }

        normalized = normalizer.normalize_issue(payload, "jira", {})
        raw = normalized["raw"]
        serialized = json.dumps(raw, ensure_ascii=False)

        for secret in (
            "top-secret-token",
            "top-secret-bearer",
            "top-secret-password",
            "embedded-secret-bearer",
            "url-secret-token",
            "url-secret-signature",
        ):
            self.assertNotIn(secret, serialized)
        self.assertEqual(raw["access_token"], normalizer.REDACTED)
        self.assertEqual(raw["authorization"], normalizer.REDACTED)
        self.assertIn(normalizer.REDACTED, raw["nested"]["note"])
        query = parse_qs(urlsplit(raw["nested"]["signed_url"]).query)
        self.assertEqual(query["token"], [normalizer.REDACTED])
        self.assertEqual(query["X-Amz-Signature"], [normalizer.REDACTED])
        self.assertEqual(query["safe"], ["visible"])

    def test_retain_raw_explicitly_preserves_original_payload(self) -> None:
        payload = {
            "id": "BUG-SECRET",
            "title": "Secret-bearing payload",
            "token": "explicit-secret",
            "authorization": "Bearer explicit-bearer",
            "url": "https://example.invalid/file?signature=explicit-signature",
        }

        normalized = normalizer.normalize_issue(payload, "jira", {}, retain_raw=True)

        self.assertEqual(normalized["raw"], payload)
        serialized = json.dumps(normalized["raw"], ensure_ascii=False)
        self.assertIn("explicit-secret", serialized)
        self.assertIn("explicit-bearer", serialized)
        self.assertIn("explicit-signature", serialized)

    def test_nested_standard_fields_and_raw_copies_are_redacted_by_default(self) -> None:
        payload = self.nested_sensitive_payload()

        normalized = normalizer.normalize_issue(payload, "jira", {})
        serialized = json.dumps(
            {
                "requirements": normalized["requirements"],
                "attachments": normalized["attachments"],
                "comments": normalized["comments"],
                "activities": normalized["activities"],
                "evidence_fetch": normalized["evidence_fetch"],
                "description": normalized["description"],
                "source_url": normalized["source_url"],
                "raw": normalized["raw"],
            },
            ensure_ascii=False,
        )

        for secret in (
            "description-token",
            "description-signature",
            "source-signature",
            "requirement-url-token",
            "requirement-raw-secret",
            "requirement-bearer",
            "attachment-signature",
            "attachment-token",
            "comment-sign",
            "comment-token",
            "comment-header-sign",
            "activity-bearer",
            "evidence-sign",
        ):
            self.assertNotIn(secret, serialized)
        self.assertIn(normalizer.REDACTED, serialized)

    def test_retain_raw_preserves_nested_standard_fields_and_raw_copies(self) -> None:
        payload = self.nested_sensitive_payload()

        normalized = normalizer.normalize_issue(payload, "jira", {}, retain_raw=True)
        serialized = json.dumps(normalized, ensure_ascii=False)

        self.assertEqual(normalized["raw"], payload)
        for secret in (
            "description-token",
            "description-signature",
            "source-signature",
            "requirement-url-token",
            "requirement-raw-secret",
            "requirement-bearer",
            "attachment-signature",
            "attachment-token",
            "comment-sign",
            "comment-token",
            "comment-header-sign",
            "activity-bearer",
            "evidence-sign",
        ):
            self.assertIn(secret, serialized)

    def test_comments_activities_and_evidence_are_canonical_top_level_fields(self) -> None:
        payload = {
            "id": "BUG-EVIDENCE",
            "title": "Evidence-rich bug",
            "comment_list": {
                "items": [
                    {
                        "comment_id": "c-2",
                        "creator": {"name": "Reporter"},
                        "create_time": "2026-07-13T10:00:00+08:00",
                        "content": {"text": "The video shows the first frame missing."},
                        "files": [{"name": "repro.mp4", "inspection_state": "inspected"}],
                    }
                ]
            },
            "op_records": [
                {
                    "record_id": "op-1",
                    "operate_time": "2026-07-13T09:00:00+08:00",
                    "description": "Issue reopened after regression.",
                }
            ],
            "evidence_review": complete_evidence("Video confirms the current reproduction."),
        }

        normalized = normalizer.normalize_issue(payload, "feishu-project", {})

        self.assertEqual(normalized["comments"][0]["id"], "c-2")
        self.assertIn("first frame", normalized["comments"][0]["content_text"])
        self.assertEqual(normalized["activities"][0]["id"], "op-1")
        self.assertEqual(normalized["evidence_fetch"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
