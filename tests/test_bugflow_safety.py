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


def sufficient_report_quality(
    *facts: str, evidence_refs: list[str] | None = None
) -> dict[str, object]:
    return {
        "status": "sufficient",
        "assessed_at": "2026-07-13T10:05:00+08:00",
        "hash_version": runner.REPORT_HASH_VERSION,
        "facts": list(facts)
        or ["复现条件、实际结果、期望结果和验收口径已由完整工单证据明确。"],
        "evidence_refs": evidence_refs or ["description"],
        "missing_fields": [],
        "conflicts": [],
        "questions": [],
        "feedback_targets": [],
        "feedback_draft": "",
    }


def bind_report_quality(issue: dict[str, object]) -> dict[str, object]:
    quality = issue.get("report_quality")
    if not isinstance(quality, dict):
        raise AssertionError("report_quality must be a dictionary before binding")
    quality["hash_version"] = runner.REPORT_HASH_VERSION
    quality["input_hash"] = runner.report_quality_input_hash(issue)
    return issue


def current_triage_metadata(
    issue: dict[str, object], **overrides: object
) -> dict[str, object]:
    quality = runner.issue_report_quality_state(issue)
    metadata: dict[str, object] = {
        "evidence_complete": runner.issue_evidence_state(issue)["complete"],
        "report_quality_complete": quality["complete"],
        "report_quality_input_hash": quality["expected_input_hash"],
        "report_quality_hash_version": runner.REPORT_HASH_VERSION,
        "triage_policy_version": runner.REPORT_QUALITY_POLICY_VERSION,
    }
    metadata.update(overrides)
    return metadata


class DistributionHygieneTests(unittest.TestCase):
    def test_skill_description_uses_trigger_focused_use_when_form(self) -> None:
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        match = re.search(r"(?m)^description:\s*(.+)$", skill_text)

        self.assertIsNotNone(match)
        self.assertTrue(match.group(1).startswith("Use when "))

    def test_generic_runtime_files_do_not_embed_numeric_custom_field_ids(self) -> None:
        public_runtime_files = (
            SKILL_ROOT / "assets" / "feishu-project-config.template.yaml",
            SKILL_ROOT / "scripts" / "normalize_issue_payload.py",
        )

        for path in public_runtime_files:
            with self.subTest(path=path.name):
                self.assertNotRegex(
                    path.read_text(encoding="utf-8"),
                    r"(?<!_)\bfield_(?=[a-z0-9]*\d)[a-z0-9]{6,}\b",
                )

    def test_local_quality_caches_are_ignored(self) -> None:
        ignored = set(
            (SKILL_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        )

        self.assertIn(".ruff_cache/", ignored)

    def test_codex_metadata_declares_non_secret_feishu_mcp_dependency(self) -> None:
        metadata = runner.load_yaml(SKILL_ROOT / "agents" / "openai.yaml")
        dependencies = metadata.get("dependencies", {}).get("tools", [])

        self.assertIn(
            {
                "type": "mcp",
                "value": "feishu-project",
                "description": "飞书 Project MCP 服务；每位用户单独配置 X-Mcp-Token",
                "transport": "streamable_http",
                "url": "https://project.feishu.cn/mcp_server/v1",
            },
            dependencies,
        )
        serialized = json.dumps(metadata, ensure_ascii=False)
        self.assertNotIn("http_headers", serialized)
        self.assertNotRegex(serialized, r"m-[0-9a-f]{8}-[0-9a-f-]{20,}")

    def test_mcp_setup_reference_covers_supported_clients_without_plaintext_token(self) -> None:
        setup = (SKILL_ROOT / "references" / "mcp-client-setup.md").read_text(
            encoding="utf-8"
        )

        for client in ("Codex", "Cursor", "Claude Code", "Other MCP Clients"):
            with self.subTest(client=client):
                self.assertIn(f"## {client}", setup)
        self.assertIn("${env:FEISHU_PROJECT_MCP_TOKEN}", setup)
        self.assertIn("${FEISHU_PROJECT_MCP_TOKEN}", setup)
        self.assertIn('env_http_headers = { "X-Mcp-Token"', setup)
        self.assertNotRegex(setup, r"m-[0-9a-f]{8}-[0-9a-f-]{20,}")

    def test_live_feishu_preflight_fails_fast_without_reconfiguring_mcp(self) -> None:
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("导出 JSON 不要求 MCP", skill_text)
        self.assertIn("不得自行安装、重配、反复探测 MCP", skill_text)
        self.assertIn("按工具 schema 和动作语义匹配", skill_text)


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
    def test_feishu_starter_keeps_resolve_as_capability_but_not_default_action(self) -> None:
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
            ["commit", "start-fix"],
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
            "report_quality": sufficient_report_quality(),
        }
        bind_report_quality(issue)

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
            "report_quality": sufficient_report_quality(),
        }
        bind_report_quality(issue)

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

    def test_complete_evidence_without_report_quality_assessment_cannot_auto_fix(self) -> None:
        issue = {
            "id": "2-unknown",
            "number": "BUG-2-UNKNOWN",
            "title": "详情页按钮颜色不一致",
            "description": "按钮颜色需要调整。",
            "evidence_fetch": complete_evidence(),
        }

        result = runner.classify_issue(self.config(), issue, self.MATCH)
        status, blockers = runner.repair_gate(
            self.config(), result | self.MATCH, approved=True
        )

        self.assertEqual(result["report_quality_status"], "unknown")
        self.assertIs(result["report_quality_complete"], False)
        self.assertEqual(result["readiness"], "ask-for-confirmation")
        self.assertNotEqual(result["readiness"], "auto-fix-candidate")
        self.assertEqual(status, "blocked")
        self.assertRegex(" ".join(blockers).lower(), r"report-quality|acceptance|expected")

    def test_missing_expected_result_produces_exact_question_and_feedback_draft(self) -> None:
        exact_question = "期望按哪个字段、什么方向排序，并应用于完整数据集还是当前页？"
        issue = {
            "id": "2-missing",
            "number": "BUG-2-MISSING",
            "title": "列表排序不对",
            "description": "当前排序不对，请修改。",
            "evidence_fetch": complete_evidence(),
            "report_quality": {
                "status": "needs-clarification",
                "assessed_at": "2026-07-13T10:05:00+08:00",
                "facts": ["当前列表顺序被报告为不正确。"],
                "evidence_refs": ["description"],
                "missing_fields": [
                    {
                        "field": "expected_result",
                        "reason": "未说明排序字段、方向和作用范围。",
                        "question": exact_question,
                        "target": "测试/产品",
                    }
                ],
            },
        }
        bind_report_quality(issue)

        quality = runner.issue_report_quality_state(issue)
        result = runner.classify_issue(self.config(), issue, self.MATCH)
        status, blockers = runner.repair_gate(
            self.config(), result | self.MATCH, approved=True
        )

        self.assertEqual(quality["status"], "needs-clarification")
        self.assertEqual(quality["questions"], [exact_question])
        self.assertIn(exact_question, quality["feedback_draft"])
        self.assertEqual(result["report_quality_questions"], [exact_question])
        self.assertIn(exact_question, result["missing_information"])
        self.assertIn(exact_question, result["feedback_draft"])
        self.assertEqual(result["feedback_publish_status"], "draft-only")
        self.assertEqual(status, "blocked")
        self.assertRegex(" ".join(blockers).lower(), r"report-quality|acceptance|expected")

    def test_comments_and_video_can_make_empty_structured_fields_sufficient(self) -> None:
        issue = {
            "id": "2-media",
            "number": "BUG-2-MEDIA",
            "title": "详情页按钮被底部栏遮挡",
            "description": "请查看评论和录屏。",
            "reproduction_steps": "",
            "actual_result": "",
            "expected_result": "",
            "acceptance_criteria": "",
            "comments": [
                {
                    "id": "comment-7",
                    "content_text": (
                        "375px 宽度打开详情页并滚动到底部，按钮被底栏遮挡；"
                        "期望按钮完整可见且可点击。"
                    ),
                    "attachments": [],
                }
            ],
            "attachments": [
                {
                    "id": "video-1",
                    "name": "repro.mp4",
                    "decision_relevant": True,
                    "inspection_state": "inspected",
                    "summary": "00:08 显示按钮被底部栏遮挡，00:11 点击无响应。",
                }
            ],
            "evidence_fetch": complete_evidence(
                "评论与录屏共同明确复现步骤、实际结果和验收口径。"
            ),
            "report_quality": sufficient_report_quality(
                "375px 详情页底部按钮必须完整可见且可点击。",
                evidence_refs=["comment comment-7", "attachment repro.mp4@00:08-00:11"],
            ),
        }
        bind_report_quality(issue)

        result = runner.classify_issue(self.config(), issue, self.MATCH)

        self.assertEqual(result["report_quality_status"], "sufficient")
        self.assertIs(result["report_quality_complete"], True)
        self.assertEqual(result["ownership"], "frontend-owned")
        self.assertEqual(result["readiness"], "auto-fix-candidate")

    def test_conflicting_acceptance_sources_block_repair(self) -> None:
        exact_question = "标题要求降序，最新评论要求升序；哪一个是最终验收口径？"
        issue = {
            "id": "2-conflict",
            "number": "BUG-2-CONFLICT",
            "title": "列表按创建时间降序",
            "description": "请调整列表顺序。",
            "comments": [
                {
                    "id": "comment-9",
                    "content_text": "最终应该按创建时间升序。",
                    "attachments": [],
                }
            ],
            "evidence_fetch": complete_evidence(),
            "report_quality": {
                "status": "conflicting",
                "assessed_at": "2026-07-13T10:05:00+08:00",
                "facts": ["标题与最新评论给出了不同的排序方向。"],
                "evidence_refs": ["title", "comment comment-9"],
                "conflicts": [
                    {
                        "topic": "创建时间排序方向",
                        "sources": ["title", "comment comment-9"],
                        "question": exact_question,
                        "target": "产品",
                    }
                ],
            },
        }
        bind_report_quality(issue)

        result = runner.classify_issue(self.config(), issue, self.MATCH)
        status, blockers = runner.repair_gate(
            self.config(), result | self.MATCH, approved=True
        )

        self.assertEqual(result["report_quality_status"], "conflicting")
        self.assertIs(result["report_quality_complete"], False)
        self.assertEqual(result["report_quality_questions"], [exact_question])
        self.assertEqual(result["readiness"], "ask-for-confirmation")
        self.assertEqual(status, "blocked")
        self.assertRegex(" ".join(blockers).lower(), r"report-quality|acceptance|expected")

    def test_non_empty_matching_report_quality_hash_is_accepted_without_crashing(self) -> None:
        issue = {
            "id": "2-hash",
            "number": "BUG-2-HASH",
            "title": "详情页按钮颜色不一致",
            "description": "按钮应使用绿色主题色。",
            "evidence_fetch": complete_evidence(),
            "report_quality": sufficient_report_quality(
                "按钮应使用绿色主题色。", evidence_refs=["description"]
            ),
        }
        bind_report_quality(issue)

        state = runner.issue_report_quality_state(issue)

        self.assertEqual(len(issue["report_quality"]["input_hash"]), 64)
        self.assertEqual(state["status"], "sufficient")
        self.assertIs(state["assessment_current"], True)
        self.assertIs(state["complete"], True)

    def test_new_comment_makes_old_assessment_stale_and_hides_old_facts(self) -> None:
        old_fact = "列表接口必须由后端按数据库字段倒序返回。"
        issue = {
            "id": "2-stale",
            "number": "BUG-2-STALE",
            "title": "详情页按钮颜色不一致",
            "description": "只调整当前页面按钮的 CSS 颜色。",
            "comments": [],
            "evidence_fetch": complete_evidence(),
            "report_quality": sufficient_report_quality(
                old_fact, evidence_refs=["description"]
            ),
        }
        bind_report_quality(issue)
        old_hash = issue["report_quality"]["input_hash"]
        issue["comments"].append(
            {
                "id": "comment-new",
                "content_text": "补充：请以最新版绿色视觉稿为准。",
                "attachments": [],
            }
        )

        quality = runner.issue_report_quality_state(issue)
        triage = runner.classify_issue(self.config(), issue, self.MATCH)
        report = runner.render_daily_markdown(
            [
                {
                    "issue": issue["number"],
                    "id": issue["id"],
                    "title": issue["title"],
                    "priority": "P2",
                    "status": "待修复",
                    "date": "2026-07-13",
                    "reporter": "测试",
                    "assignee": "当前用户",
                    "repository_match": "current-repo",
                    "confirmation_required": False,
                    **triage,
                    "questions": triage["report_quality_questions"],
                }
            ]
        )

        self.assertNotEqual(old_hash, quality["expected_input_hash"])
        self.assertEqual(quality["status"], "unknown")
        self.assertIs(quality["assessment_current"], False)
        self.assertEqual(quality["facts"], [])
        self.assertEqual(triage["ownership"], "frontend-owned")
        self.assertEqual(triage["report_quality_facts"], [])
        self.assertNotIn(old_fact, report)
        self.assertIn("未评估", report)

    def test_approved_does_not_bypass_unresolved_confirmation(self) -> None:
        item = {
            "repository_match": "current-repo",
            "ownership": "needs-confirmation",
            "confirmation_required": True,
            "readiness": "ask-for-confirmation",
            "effort": "easy",
            "report_quality_complete": True,
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
            "report_quality_complete": True,
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
            "report_quality_complete": True,
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
            "actual_result": "接口按 created_at asc 返回。",
            "expected_result": "接口按 created_at desc、id desc 返回完整数据集。",
            "acceptance_criteria": "跨分页查询仍保持 created_at desc、id desc。",
            "implementation_suggestion": "前端拿到当前页后调用 reverse。",
            "evidence_fetch": complete_evidence(),
            "report_quality": sufficient_report_quality(
                "期望接口按 created_at desc、id desc 返回完整数据集。"
            ),
        }
        bind_report_quality(issue)

        result = runner.classify_issue(self.config(), issue, self.MATCH)
        status, blockers = runner.repair_gate(self.config(), result | self.MATCH, approved=True)

        self.assertEqual(result["ownership"], "backend-owned")
        self.assertEqual(result["readiness"], "redirect-to-owner")
        self.assertEqual(result["report_quality_status"], "sufficient")
        self.assertIs(result["report_quality_complete"], True)
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
            "report_quality": sufficient_report_quality(),
        }
        bind_report_quality(issue)

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
            "report_quality": sufficient_report_quality(
                "最新评论明确了期望排序。", evidence_refs=["comment comment-1"]
            ),
        }
        bind_report_quality(issue)

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
            "report_quality": sufficient_report_quality(
                "录屏明确展示按钮被底部栏遮挡。",
                evidence_refs=["attachment repro.mp4@00:08"],
            ),
        }
        bind_report_quality(issue)

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
            "report_quality_complete": True,
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

    def test_unknown_attachment_container_shape_fails_closed(self) -> None:
        issue = {
            "attachments": {"unexpected": {"name": "repro.png"}},
            "comments": [
                {
                    "id": "comment-attachment-container",
                    "content_text": "附件见评论。",
                    "attachments": {"unexpected": {"name": "comment.png"}},
                }
            ],
            "evidence_fetch": complete_evidence(),
        }

        result = runner.issue_evidence_state(issue)

        self.assertFalse(result["complete"])
        self.assertEqual(result["status"], "partial")
        self.assertIn(
            "attachment container shape is not a readable list",
            result["missing"],
        )

    def test_inspected_attachments_without_factual_summaries_fail_closed(self) -> None:
        issue = {
            "attachments": [
                {
                    "name": "top-level.png",
                    "decision_relevant": True,
                    "inspection_state": "inspected",
                    "summary": "",
                }
            ],
            "comments": [
                {
                    "id": "comment-empty-summary",
                    "content_text": "评论附件已打开。",
                    "attachments": [
                        {
                            "name": "comment.mp4",
                            "decision_relevant": True,
                            "inspection_state": "inspected",
                        }
                    ],
                }
            ],
            "evidence_fetch": complete_evidence(),
        }

        result = runner.issue_evidence_state(issue)

        self.assertFalse(result["complete"])
        self.assertEqual(result["status"], "partial")
        self.assertIn(
            "attachment top-level.png was inspected but its factual summary is empty",
            result["missing"],
        )
        self.assertIn(
            "attachment comment.mp4 was inspected but its factual summary is empty",
            result["missing"],
        )

    def test_new_comment_or_media_finding_changes_plan_fingerprint(self) -> None:
        issue = {
            "id": "7",
            "number": "BUG-7",
            "title": "详情页错位",
            "comments": [{"id": "comment-1", "content_text": "首次复现。", "attachments": []}],
            "evidence_fetch": complete_evidence("Screenshot shows a narrow-screen overflow."),
            "report_quality": sufficient_report_quality(),
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
                "report_quality_complete": True,
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

    def test_daily_report_shows_information_quality_and_feedback_draft(self) -> None:
        exact_question = "请明确保存后应保留哪些字段，以及以哪个页面状态作为验收通过。"
        feedback_draft = (
            "已核对 BUG-DAILY 的详情、评论和附件，当前信息尚不足以进入修复。\n"
            f"1. {exact_question}"
        )
        report = runner.render_daily_markdown(
            [
                {
                    "issue": "BUG-DAILY",
                    "id": "daily-1",
                    "title": "保存结果不明确",
                    "priority": "P1",
                    "status": "待修复",
                    "date": "2026-07-13",
                    "reporter": "测试同学",
                    "assignee": "当前用户",
                    "repository_match": "current-repo",
                    "ownership": "frontend-owned",
                    "readiness": "ask-for-confirmation",
                    "effort": "blocked",
                    "risk": "medium",
                    "confirmation_required": False,
                    "evidence_complete": True,
                    "report_quality_status": "needs-clarification",
                    "report_quality_complete": False,
                    "report_quality_assessed_at": "2026-07-13T10:05:00+08:00",
                    "report_quality_assessment_current": True,
                    "report_quality_facts": ["保存操作已执行，但返回页面后的保留范围不明确。"],
                    "report_quality_evidence_refs": ["description", "comment comment-1"],
                    "report_quality_missing_fields": [
                        {
                            "field": "acceptance_criteria",
                            "reason": "没有可判定通过的验收口径。",
                            "question": exact_question,
                        }
                    ],
                    "report_quality_conflicts": [],
                    "questions": [exact_question],
                    "feedback_targets": ["测试/产品"],
                    "feedback_draft": feedback_draft,
                    "feedback_publish_status": "draft-only",
                }
            ]
        )

        self.assertIn("工单信息", report)
        self.assertIn("待补充", report)
        self.assertIn("## 工单描述待补充", report)
        self.assertIn(exact_question, report)
        self.assertIn(feedback_draft, report)
        self.assertIn("发布状态: draft-only", report)


class AssigneeFilterTests(unittest.TestCase):
    def test_exported_json_defaults_to_current_assignee_only(self) -> None:
        config = {"query_policy": {"assigned_to": "current-user", "assignee_aliases": ["user-7"]}}
        args = argparse.Namespace(assignee=None, include_all_assignees=False)
        tokens, mode = runner.import_assignee_filter(config, args, "jira")
        included, summary = runner.filter_imported_issues(
            [
                {"number": "BUG-1", "assignee": "current-user"},
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

    def test_native_current_user_rejects_mixed_assignee_batch(self) -> None:
        issues = [
            {"id": "1", "assignee": [{"name": "Alice"}]},
            {"id": "2", "assignee": [{"name": "Bob"}]},
        ]

        with self.assertRaisesRegex(SystemExit, "mixed assignees"):
            runner.filter_imported_issues(issues, set(), "native-query-current-user")

    def test_native_current_user_accepts_shared_identity_in_coassignee_batch(self) -> None:
        issues = [
            {"id": "1", "assignee": [{"name": "Current User"}, {"name": "Alice"}]},
            {"id": "2", "assignee": [{"name": "Current User"}, {"name": "Bob"}]},
        ]

        included, summary = runner.filter_imported_issues(
            issues, set(), "native-query-current-user"
        )

        self.assertEqual(included, issues)
        self.assertEqual(summary["mode"], "native-query-current-user-verified")
        self.assertEqual(summary["assignees"], ["current user"])


class PreviewWorkflowTests(unittest.TestCase):
    @staticmethod
    def config() -> dict[str, object]:
        return {
            "project": {"name": "web-client", "role_assumption": "frontend"},
            "issue_source": {"platform": "jira"},
            "query_policy": {
                "assigned_to": "current-user",
                "assignee_aliases": ["user-7"],
            },
            "requirement_mapping": {
                "current_repo": {
                    "repo_key": "web-client",
                    "path": ".",
                    "aliases": ["web client"],
                },
                "related_repositories": [],
                "demand_rules": [],
            },
            "execution_policy": {
                "auto_fix_allowed": True,
                "auto_fix_low_risk_frontend": True,
                "max_auto_fix_effort": "medium",
            },
        }

    @staticmethod
    def args(input_path: Path, root: Path, *, json_output: bool) -> argparse.Namespace:
        return argparse.Namespace(
            config="unused",
            local_config="",
            root=str(root),
            input=str(input_path),
            platform="jira",
            assignee=None,
            include_all_assignees=False,
            report="",
            json=json_output,
        )

    def test_preview_is_fast_provisional_and_writes_no_bugflow_artifacts(self) -> None:
        payload = [
            {
                "id": "preview-1",
                "key": "BUG-PREVIEW-1",
                "title": "web client 按钮颜色不一致",
                "description": "当前页按钮颜色与设计稿不一致。",
                "assignee": "current-user",
                "requirements": [{"id": "REQ-1", "title": "web client"}],
            },
            {
                "id": "preview-2",
                "key": "BUG-PREVIEW-2",
                "title": "其他负责人的问题",
                "description": "不应进入本次扫描。",
                "assignee": "someone-else",
                "requirements": [{"id": "REQ-2", "title": "web client"}],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree = Path(temp_dir)
            input_path = worktree / "issues.json"
            input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            root = worktree / ".bugflow/issues"
            output = io.StringIO()

            with mock.patch.object(
                runner, "load_config", return_value=self.config()
            ), mock.patch.object(
                runner,
                "report_quality_input_hash",
                side_effect=AssertionError("preview must not calculate a strict report-quality hash"),
            ), mock.patch.object(
                runner,
                "issue_evidence_state",
                side_effect=AssertionError("preview must not run the strict evidence gate"),
            ), contextlib.redirect_stdout(output):
                result = runner.preview(self.args(input_path, root, json_output=True))

            preview = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(preview["mode"], "preview")
            self.assertEqual(preview["count"], 1)
            self.assertEqual(preview["assignee_filter"]["skipped_assignee_count"], 1)
            item = preview["items"][0]
            self.assertEqual(item["issue"], "BUG-PREVIEW-1")
            self.assertIs(item["provisional"], True)
            self.assertIs(item["repair_allowed"], False)
            self.assertEqual(item["next_step"], "fix-ready")
            self.assertEqual(item["preview_report_quality"], "not-assessed")
            self.assertNotIn("plan_fingerprint", item)
            self.assertNotIn("readiness", item)
            self.assertNotIn("evidence_status", item)
            self.assertNotIn("auto-fix-candidate", json.dumps(item, ensure_ascii=False))
            self.assertFalse(root.exists())

            report = runner.render_preview_markdown(
                preview["items"], preview["assignee_filter"]
            )
            self.assertIn("初步判断", report)
            self.assertIn("fix-ready", report)

    def test_preview_result_cannot_authorize_plan_or_repair(self) -> None:
        item = {
            "issue": "BUG-PREVIEW-GATE",
            "repository_match": "current-repo",
            "ownership": "frontend-owned",
            "readiness": "auto-fix-candidate",
            "effort": "easy",
            "risk": "low",
            "provisional": True,
            "repair_allowed": False,
            "next_step": "fix-ready",
        }

        status, blockers = runner.repair_gate(self.config(), item, approved=True)

        self.assertEqual(status, "blocked")
        self.assertRegex(
            " ".join(blockers).lower(), r"evidence|report-quality|acceptance"
        )
        self.assertNotIn("plan_fingerprint", item)


class ExistingDailyWorkflowTests(unittest.TestCase):
    @staticmethod
    def config(root: Path) -> dict[str, object]:
        return {
            "project": {"name": "web-client", "role_assumption": "frontend"},
            "bugflow": {"root": str(root)},
            "requirement_mapping": {
                "current_repo": {
                    "repo_key": "web-client",
                    "path": ".",
                    "aliases": ["web client"],
                },
                "related_repositories": [],
                "demand_rules": [],
            },
            "execution_policy": {
                "auto_fix_allowed": True,
                "auto_fix_low_risk_frontend": True,
            },
        }

    @staticmethod
    def issue(number: str) -> dict[str, object]:
        return bind_report_quality(
            {
                "source": "test",
                "id": number.lower(),
                "number": number,
                "title": "web client 按钮颜色不一致",
                "description": "只调整 web client 当前页面按钮的 CSS 颜色。",
                "status": "待修复",
                "assignee": "current-user",
                "requirements": [{"id": "REQ-1", "title": "web client"}],
                "evidence_fetch": complete_evidence(),
                "report_quality": sufficient_report_quality(
                    "按钮颜色的实际值与期望主题色已明确。",
                    evidence_refs=["description"],
                ),
            }
        )

    def test_daily_existing_only_triages_explicit_issue_without_overwriting_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / ".bugflow/issues"
            selected = self.issue("BUG-SELECTED")
            historical = self.issue("BUG-HISTORICAL")
            selected_dir = runner.write_issue_json(root, selected)
            historical_dir = runner.write_issue_json(root, historical)
            selected_before = (selected_dir / "issue.json").read_bytes()
            historical_triage_before = (historical_dir / "triage.md").read_bytes()
            args = argparse.Namespace(
                config="unused",
                local_config="",
                root=str(root),
                issue=["BUG-SELECTED"],
                report="",
                assignee=["current-user"],
                include_all_assignees=False,
            )

            with mock.patch.object(
                runner, "load_config", return_value=self.config(root)
            ), contextlib.redirect_stdout(io.StringIO()):
                result = runner.daily_existing(args)

            selected_after = json.loads(
                (selected_dir / "issue.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result, 0)
            self.assertEqual((selected_dir / "issue.json").read_bytes(), selected_before)
            self.assertEqual(selected_after["report_quality"], selected["report_quality"])
            self.assertEqual(
                runner.artifact_frontmatter_status(selected_dir / "triage.md"), "done"
            )
            self.assertEqual(
                (historical_dir / "triage.md").read_bytes(), historical_triage_before
            )


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

    def test_standard_mode_requires_the_named_plan_checks(self) -> None:
        args = argparse.Namespace(
            mode=runner.STANDARD_VERIFICATION_MODE,
            command=["echo looks-good => passed"],
            check=None,
            browser="not-required",
            blocked="",
            failed=False,
            evidence=None,
        )

        self.assertEqual(
            runner.verification_status(args, ["lint"], {}),
            "pending",
        )
        args.check = ["lint=passed: exact configured lint completed"]
        self.assertEqual(
            runner.verification_status(args, ["lint"], {}),
            "done",
        )

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
                "project_key": "example-space",
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

        self.assertIn("FROM `example-space`.`issue`", result["mql"])
        self.assertIn("ORDER BY `updated_at` DESC", result["mql"])
        self.assertIn("LIMIT 20", result["mql"])

    def test_project_key_with_backtick_is_rejected(self) -> None:
        config = self.config()
        config["issue_source"]["project_key"] = "example-space` UNION SELECT"

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

    def test_profiles_exclude_unverified_optional_fields(self) -> None:
        config = self.config()
        config["field_mapping"].update(
            {
                "reporter": "issue_reporter",
                "requirements": "linked_requirement",
                "attachments": "attachment_field",
            }
        )

        preview = runner.build_feishu_mql(config, profile="preview")
        fix_ready = runner.build_feishu_mql(config, profile="fix-ready")

        self.assertNotIn("issue_reporter", preview["select_fields"])
        self.assertNotIn("issue_reporter", fix_ready["select_fields"])
        self.assertIn("issue_reporter", fix_ready["unverified_optional_fields"])

        config["field_verification"] = {
            "verified_keys": ["issue_reporter", "linked_requirement", "attachment_field"]
        }
        verified = runner.build_feishu_mql(config, profile="fix-ready")
        self.assertIn("issue_reporter", verified["select_fields"])
        self.assertIn("attachment_field", verified["select_fields"])

    def test_requirement_scope_uses_post_filter_until_pushdown_is_verified(self) -> None:
        config = self.config()
        config["field_mapping"]["requirements"] = "linked_requirement"
        config["query_policy"]["requirement_ids"] = ["REQ-42"]

        post_filtered = runner.build_feishu_mql(config)
        self.assertTrue(post_filtered["requirement_post_filter_required"])
        self.assertNotIn("REQ-42", post_filtered["mql"])

        config["query_policy"]["requirement_mql_pushdown_verified"] = True
        with self.assertRaisesRegex(SystemExit, "remotely verified"):
            runner.build_feishu_mql(config)

        config["field_verification"] = {"verified_keys": ["linked_requirement"]}
        pushed = runner.build_feishu_mql(config)
        self.assertTrue(pushed["requirement_filter_pushed_down"])
        self.assertIn("REQ-42", pushed["mql"])


class WorkflowDependencyTests(unittest.TestCase):
    @staticmethod
    def issue() -> dict[str, object]:
        return bind_report_quality({
            "source": "test",
            "id": "7",
            "number": "BUG-7",
            "title": "Dependency gate",
            "requirements": [],
            "evidence_fetch": complete_evidence(),
            "report_quality": sufficient_report_quality(),
        })

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
            issue = self.issue()
            target = runner.write_issue_json(root, issue)
            runner.write_markdown_artifact(
                runner.artifact_path(target, "requirement-match"),
                "requirement-match",
                "done",
                "# requirement-match\n",
            )
            runner.write_markdown_artifact(
                runner.artifact_path(target, "triage-report"),
                "triage-report",
                "done",
                "# triage-report\n",
                current_triage_metadata(issue),
            )
            runner.write_markdown_artifact(
                runner.artifact_path(target, "fix-plan"),
                "fix-plan",
                "done",
                "# fix-plan\n",
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
            issue = self.issue()
            target = runner.write_issue_json(root, issue)
            (worktree / "src").mkdir()
            (worktree / "src/file.py").write_text("print('change')\n", encoding="utf-8")
            runner.write_markdown_artifact(
                runner.artifact_path(target, "requirement-match"),
                "requirement-match",
                "done",
                "# requirement-match\n",
            )
            runner.write_markdown_artifact(
                runner.artifact_path(target, "triage-report"),
                "triage-report",
                "done",
                "# triage-report\n",
                current_triage_metadata(issue),
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

    def test_legacy_approved_artifacts_without_report_quality_cannot_continue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree = Path(temp_dir)
            root = worktree / "issues"
            issue = self.issue()
            target = runner.write_issue_json(root, issue)
            (worktree / "src").mkdir()
            (worktree / "src/file.py").write_text("print('change')\n", encoding="utf-8")
            runner.write_markdown_artifact(
                runner.artifact_path(target, "requirement-match"),
                "requirement-match",
                "done",
                "# legacy matched\n",
            )
            runner.write_markdown_artifact(
                runner.artifact_path(target, "triage-report"),
                "triage-report",
                "done",
                "# legacy triage without report-quality metadata\n",
                {
                    "repository_match": "current-repo",
                    "confidence": "high",
                    "confirmation_required": False,
                    "ownership": "frontend-owned",
                    "effort": "easy",
                    "readiness": "manual-review-first",
                    "risk": "low",
                    "evidence_complete": True,
                },
            )
            runner.write_markdown_artifact(
                runner.artifact_path(target, "fix-plan"),
                "fix-plan",
                "done",
                "# legacy approved plan\n",
                {
                    "plan_fingerprint": "legacy-approved-plan",
                    "fix_approved": True,
                    "planned_files": ["src/file.py"],
                },
            )
            args = argparse.Namespace(
                config="unused",
                local_config="",
                root=str(root),
                issue="BUG-7",
                summary=["Must not reuse legacy approval"],
                files=["src/file.py"],
                remote_status="未修改",
                commit="",
                notes="",
                blocked="",
            )

            with working_directory(worktree), mock.patch.object(
                runner, "load_config", return_value=self.config(root)
            ):
                self.assertNotEqual(
                    runner.artifact_effective_status(target, "triage-report"),
                    "done",
                )
                self.assertNotEqual(
                    runner.artifact_effective_status(target, "fix-plan"),
                    "done",
                )
                with self.assertRaises(SystemExit) as raised:
                    with contextlib.redirect_stdout(io.StringIO()):
                        runner.record_implementation(args)

            self.assertRegex(
                str(raised.exception).lower(),
                r"report.?quality|triage|fix.?plan|upstream|stale|current|regenerate",
            )
            self.assertNotEqual(
                runner.artifact_frontmatter_status(target / "implementation.md"), "done"
            )

    def test_record_implementation_must_match_approved_plan_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree = Path(temp_dir)
            root = worktree / "issues"
            issue = self.issue()
            target = runner.write_issue_json(root, issue)
            (worktree / "src").mkdir()
            (worktree / "src/planned.py").write_text("print('planned')\n", encoding="utf-8")
            (worktree / "src/other.py").write_text("print('other')\n", encoding="utf-8")
            runner.write_markdown_artifact(
                runner.artifact_path(target, "requirement-match"),
                "requirement-match",
                "done",
                "# requirement-match\n",
            )
            runner.write_markdown_artifact(
                runner.artifact_path(target, "triage-report"),
                "triage-report",
                "done",
                "# triage-report\n",
                current_triage_metadata(issue),
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
            issue = self.issue()
            target = runner.write_issue_json(root, issue)
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
                current_triage_metadata(
                    issue,
                    repository_match="current-repo",
                    confidence="high",
                    confirmation_required=False,
                    ownership="frontend-owned",
                    effort="medium",
                    readiness="manual-review-first",
                    risk="medium",
                ),
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
                verified_by="agent",
                verification_note="Reviewed the approved lightweight scope.",
                check=None,
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

    def test_lightweight_verification_requires_complete_report_quality(self) -> None:
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
                    "effort": "easy",
                    "readiness": "manual-review-first",
                    "risk": "low",
                    "evidence_complete": True,
                    "report_quality_complete": False,
                    "triage_policy_version": runner.REPORT_QUALITY_POLICY_VERSION,
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
            args = argparse.Namespace(mode=runner.LIGHTWEIGHT_VERIFICATION_MODE)
            config = {
                "execution_policy": {"allow_lightweight_verification": True},
            }

            with mock.patch.object(
                runner, "artifact_effective_status", return_value="done"
            ):
                blockers = runner.lightweight_verification_blockers(config, target, args)

            self.assertRegex(
                " ".join(blockers).lower(), r"issue information|implementation|acceptance"
            )


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
                "allow_deferred_user_verification": True,
                "approved_completion_actions": ["commit"],
                "assisted_completion_actions": ["commit", "start-fix"],
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
        return bind_report_quality({
            "source": "test",
            "id": "1",
            "number": "BUG-1",
            "title": "列表按钮颜色偏差",
            "description": "只修改 web client 的按钮 CSS 颜色。",
            "status": "OPEN",
            "requirements": [{"id": "REQ-1", "title": "web client"}],
            "evidence_fetch": complete_evidence(),
            "report_quality": sufficient_report_quality(),
        })

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
        deferred_to_user: bool = False,
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
                runner.DEFERRED_USER_VERIFICATION_MODE
                if deferred_to_user
                else (
                    runner.LIGHTWEIGHT_VERIFICATION_MODE
                    if lightweight
                    else runner.STANDARD_VERIFICATION_MODE
                )
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
                runner.DEFERRED_USER_VERIFICATION_MODE
                if deferred_to_user
                else (
                    runner.LIGHTWEIGHT_VERIFICATION_MODE
                    if lightweight
                    else runner.STANDARD_VERIFICATION_MODE
                )
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
            verified_by="agent",
            verification_note="Recorded by the workflow test agent.",
            check=None,
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
                if not deferred_to_user:
                    self.assertEqual(runner.record_verification(verification_args), 0)
        return issue

    def test_assisted_plan_defers_checks_and_uses_human_handoff_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            config["execution_policy"]["approved_completion_actions"] = [
                "commit",
                "start-fix",
                "resolve-for-acceptance",
            ]
            with working_directory(repo):
                self.prepare_verified_issue(root, config, deferred_to_user=True)
                target = runner.issue_dir(root, "BUG-1")
                metadata = runner.frontmatter_metadata(
                    runner.artifact_path(target, "fix-plan")
                )

                self.assertEqual(
                    metadata["verification_mode"],
                    runner.DEFERRED_USER_VERIFICATION_MODE,
                )
                self.assertEqual(json.loads(metadata["required_checks"]), [])
                self.assertEqual(
                    set(json.loads(metadata["completion_actions"])),
                    {"commit", "start-fix"},
                )

    def test_assisted_legacy_bundle_never_inherits_resolve_action(self) -> None:
        config = {
            "execution_policy": {
                "approved_completion_actions": [
                    "commit",
                    "start-fix",
                    "resolve-for-acceptance",
                ]
            }
        }
        args = argparse.Namespace(
            completion_action=None,
            verification_mode=runner.DEFERRED_USER_VERIFICATION_MODE,
        )

        self.assertEqual(
            runner.normalize_completion_actions(config, args),
            ["commit", "start-fix"],
        )

    def test_autonomous_legacy_bundle_never_inherits_resolve_action(self) -> None:
        config = {
            "execution_policy": {
                "approved_completion_actions": [
                    "commit",
                    "start-fix",
                    "resolve-for-acceptance",
                ]
            }
        }
        args = argparse.Namespace(
            completion_action=None,
            verification_mode=runner.STANDARD_VERIFICATION_MODE,
        )

        self.assertEqual(
            runner.normalize_completion_actions(config, args),
            ["commit", "start-fix"],
        )

    def test_assisted_mode_commits_before_user_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            baseline = self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            with working_directory(repo):
                self.prepare_verified_issue(root, config, deferred_to_user=True)
                (repo / "target.txt").write_text(
                    "awaiting human verification\n", encoding="utf-8"
                )

                output = io.StringIO()
                with mock.patch.object(
                    runner, "load_config", return_value=config
                ), contextlib.redirect_stdout(output):
                    self.assertEqual(runner.commit_fix(self.commit_args(root)), 0)

                result = json.loads(output.getvalue())
                self.assertNotEqual(
                    self.git(repo, "rev-parse", "HEAD").stdout.strip(), baseline
                )
                self.assertTrue(result["verification_pending"])
                self.assertEqual(
                    result["verification_mode"],
                    runner.DEFERRED_USER_VERIFICATION_MODE,
                )

    def test_deferred_verification_requires_direct_user_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            with working_directory(repo):
                self.prepare_verified_issue(root, config, deferred_to_user=True)
                common = {
                    "config": "unused",
                    "local_config": "",
                    "root": str(root),
                    "issue": "BUG-1",
                    "mode": runner.DEFERRED_USER_VERIFICATION_MODE,
                    "confidence": "",
                    "exemption_reason": "",
                    "command": None,
                    "check": ["acceptance=passed: 人工确认原问题已修复"],
                    "browser": "not-required",
                    "browser_note": "",
                    "evidence": None,
                    "residual_risk": "无",
                    "verification_note": "用户在当前任务中确认验收通过。",
                    "failed": False,
                    "blocked": "",
                }

                with mock.patch.object(runner, "load_config", return_value=config):
                    with self.assertRaises(SystemExit) as raised:
                        runner.record_verification(
                            argparse.Namespace(**common, verified_by="agent")
                        )
                    self.assertRegex(str(raised.exception).lower(), r"user|human")

                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(
                            runner.record_verification(
                                argparse.Namespace(**common, verified_by="user")
                            ),
                            0,
                        )

                result = json.loads(output.getvalue())
                self.assertEqual(result["status"], "done")
                metadata = runner.frontmatter_metadata(
                    runner.artifact_path(
                        runner.issue_dir(root, "BUG-1"), "verification"
                    )
                )
                self.assertEqual(metadata["human_verified"], "true")

    def test_deferred_commit_respects_project_deny(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            baseline = self.create_repo(repo)
            root = repo / ".bugflow/issues"
            config = self.workflow_config(root)
            config["execution_policy"]["allow_deferred_user_verification"] = False
            with working_directory(repo):
                self.prepare_verified_issue(root, config, deferred_to_user=True)
                (repo / "target.txt").write_text(
                    "disallowed deferred commit\n", encoding="utf-8"
                )

                with mock.patch.object(runner, "load_config", return_value=config):
                    with self.assertRaises(SystemExit) as raised:
                        runner.commit_fix(self.commit_args(root))

                self.assertRegex(
                    str(raised.exception).lower(), r"deferred|user verification|policy"
                )
                self.assertEqual(
                    self.git(repo, "rev-parse", "HEAD").stdout.strip(), baseline
                )

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
                    r"upstream|implementation|fix.?plan|verification|stale|triage|report.?quality|policy",
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


class WorkflowHardeningTests(unittest.TestCase):
    def test_grouped_feishu_mcp_response_extracts_each_record(self) -> None:
        payload = {
            "data": {
                "group-open": [
                    {"moql_field_list": [{"field_key": "work_item_id", "value": "1"}]},
                    {"moql_field_list": [{"field_key": "work_item_id", "value": "2"}]},
                ]
            }
        }

        records = runner.iter_payload_items(payload)

        self.assertEqual(len(records), 2)
        self.assertTrue(all("moql_field_list" in record for record in records))

    def test_flat_data_records_keep_nested_people_inside_the_issue(self) -> None:
        payload = {
            "data": [
                {
                    "work_item_id": "1",
                    "name": "Nested assignee",
                    "current_status_operator": [{"name": "Current User"}],
                }
            ]
        }

        records = runner.iter_payload_items(payload)

        self.assertEqual(records, payload["data"])
        self.assertEqual(records[0]["current_status_operator"][0]["name"], "Current User")

    def test_requirement_post_filter_matches_id_number_or_url(self) -> None:
        issues = [
            {"id": "1", "requirements": [{"id": "REQ-1", "number": "1001"}]},
            {"id": "2", "requirements": [{"url": "https://tracker/REQ-2"}]},
            {"id": "3", "requirements": []},
        ]

        included, summary = runner.filter_requirement_scope(
            issues, {"1001", "https://tracker/req-2"}
        )

        self.assertEqual([item["id"] for item in included], ["1", "2"])
        self.assertEqual(summary["skipped_requirement_count"], 1)

    def test_repo_and_artifact_roots_do_not_depend_on_process_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as other_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            args = argparse.Namespace(repo_root=str(repo), artifact_root="", root="")
            config = {"project": {"repo_path": "."}, "bugflow": {"root": ".bugflow/issues"}}

            with working_directory(Path(other_dir)):
                self.assertEqual(runner.repository_root(config, args), repo.resolve())
                self.assertEqual(
                    runner.artifact_root(config, args),
                    (repo / ".bugflow/issues").resolve(),
                )

    def test_preview_report_cannot_overwrite_issue_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            args = argparse.Namespace(
                repo_root=str(repo),
                artifact_root="",
                root="",
                report=".bugflow/issues/BUG-1/triage.md",
            )
            config = {
                "project": {"repo_path": "."},
                "bugflow": {
                    "root": ".bugflow/issues",
                    "report_root": ".bugflow/reports",
                },
            }

            with self.assertRaises(SystemExit):
                runner.write_report(config, args, "unsafe")

            args.report = ".bugflow/reports/preview.md"
            path = runner.write_report(config, args, "safe")
            self.assertEqual(path.read_text(encoding="utf-8"), "safe")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_compatible_report_hash_migration_preserves_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "issues"
            issue = {
                "id": "BUG-MIGRATE",
                "number": "BUG-MIGRATE",
                "title": "Legacy assessment",
                "requirements": [],
                "evidence_fetch": complete_evidence(),
                "report_quality": {
                    "status": "sufficient",
                    "assessed_at": "2026-07-13T10:05:00+08:00",
                    "facts": ["Current evidence is sufficient."],
                    "evidence_refs": ["description"],
                    "missing_fields": [],
                    "conflicts": [],
                    "questions": [],
                },
            }
            issue["report_quality"]["input_hash"] = runner.report_quality_input_hash(issue)
            runner.write_issue_json(root, issue)
            args = argparse.Namespace(
                config="unused",
                local_config="",
                repo_root="",
                artifact_root="",
                root=str(root),
                issue="BUG-MIGRATE",
            )

            with mock.patch.object(
                runner, "load_config", return_value={"bugflow": {"root": str(root)}}
            ), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.migrate_artifacts(args), 0)

            migrated = json.loads(
                (runner.issue_dir(root, "BUG-MIGRATE") / "issue.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                migrated["report_quality"]["hash_version"], runner.REPORT_HASH_VERSION
            )
            self.assertEqual(migrated["report_quality"]["status"], "sufficient")
            self.assertEqual(
                migrated["_bugflow_meta"]["artifact_schema_version"],
                runner.ARTIFACT_SCHEMA_VERSION,
            )


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

    def test_report_quality_states_are_normalized(self) -> None:
        cases = {
            "ready": "sufficient",
            "clarification-required": "needs-clarification",
            "conflict": "conflicting",
            "not-a-real-state": "unknown",
        }

        for raw_status, expected in cases.items():
            with self.subTest(raw_status=raw_status):
                quality = normalizer.normalize_report_quality({"status": raw_status})
                self.assertEqual(quality["status"], expected)

    def test_report_quality_hash_is_stable_after_second_requirement_normalization(self) -> None:
        payload = {
            "id": "BUG-REQ-HASH",
            "number": "BUG-REQ-HASH",
            "title": "Requirement hash stability",
            "description": "The linked requirement is already normalized on the second pass.",
            "requirements": [
                {
                    "work_item_id": "req-7",
                    "auto_number": "REQ-7",
                    "name": "web client detail page",
                    "web_url": "https://example.invalid/requirements/7",
                }
            ],
            "evidence_fetch": complete_evidence(),
        }

        first = normalizer.normalize_issue(payload, "jira", {})
        second = normalizer.normalize_issue(first, "jira", {})

        self.assertEqual(first["requirements"][0]["id"], "req-7")
        self.assertEqual(first["requirements"], second["requirements"])
        self.assertEqual(
            runner.report_quality_input_hash(first),
            runner.report_quality_input_hash(second),
        )

    def test_full_width_secret_assignments_and_mcp_url_are_redacted(self) -> None:
        payload = {
            "id": "BUG-FULL-WIDTH-SECRETS",
            "title": "Secret redaction with Chinese punctuation",
            "description": (
                "密码：full-width-password\n"
                "令牌＝full-width-token\n"
                "MCP URL：https://mcp.example.invalid/connect?token=mcp-query-token"
            ),
        }

        normalized = normalizer.normalize_issue(payload, "jira", {})
        serialized = json.dumps(normalized, ensure_ascii=False)

        for secret in (
            "full-width-password",
            "full-width-token",
            "mcp.example.invalid",
            "mcp-query-token",
        ):
            self.assertNotIn(secret, serialized)
        self.assertGreaterEqual(serialized.count(normalizer.REDACTED), 3)

    def test_new_report_fields_and_quality_are_canonical_and_redacted(self) -> None:
        payload = {
            "id": "BUG-QUALITY",
            "title": "Report quality normalization",
            "steps_to_reproduce": (
                "Open https://example.invalid/repro?token=repro-secret then save."
            ),
            "actual_behavior": "Request logged Bearer actual-secret",
            "expected_behavior": "Saved values remain visible after reopening.",
            "acceptance": "Reopen twice and all saved values remain visible.",
            "test_environment": "Chrome 136, 375px viewport",
            "sample_data": "https://example.invalid/item?id=7&signature=data-secret",
            "suggested_fix": "Frontend could reverse the current page.",
            "issue_quality": {
                "status": "clarification-required",
                "facts": ["Save succeeds but the reopened view is unclear."],
                "evidence_refs": [
                    "https://example.invalid/comment?token=evidence-secret"
                ],
                "missing_fields": {
                    "field": "expected_result",
                    "reason": "Expected value is not listed.",
                    "question": (
                        "Which value should appear? "
                        "https://example.invalid/question?sign=question-secret"
                    ),
                    "target": "tester",
                },
                "feedback_targets": "tester",
                "feedback_draft": "Bearer feedback-secret",
            },
        }

        normalized = normalizer.normalize_issue(payload, "jira", {})
        quality = normalized["report_quality"]
        serialized = json.dumps(normalized, ensure_ascii=False)

        self.assertNotIn("repro-secret", normalized["reproduction_steps"])
        self.assertIn(normalizer.REDACTED, normalized["actual_result"])
        self.assertEqual(
            normalized["expected_result"],
            "Saved values remain visible after reopening.",
        )
        self.assertEqual(
            normalized["acceptance_criteria"],
            "Reopen twice and all saved values remain visible.",
        )
        self.assertEqual(normalized["environment"], "Chrome 136, 375px viewport")
        self.assertNotIn("data-secret", normalized["test_data"])
        self.assertEqual(
            normalized["implementation_suggestion"],
            "Frontend could reverse the current page.",
        )
        self.assertEqual(quality["status"], "needs-clarification")
        self.assertEqual(quality["missing_fields"][0]["field"], "expected_result")
        self.assertEqual(quality["feedback_targets"], ["tester"])
        for secret in (
            "repro-secret",
            "actual-secret",
            "data-secret",
            "evidence-secret",
            "question-secret",
            "feedback-secret",
        ):
            self.assertNotIn(secret, serialized)

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
