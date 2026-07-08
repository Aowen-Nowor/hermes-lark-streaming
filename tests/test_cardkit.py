"""cardkit.py 测试 — markdown 优化、表格处理、卡片构建."""

from __future__ import annotations

from hermes_lark_streaming.cardkit import (
    _LOADING_HINT_ELEMENT_ID,
    _build_error_panel,
    _build_footer_elements,
    _compact,
    _count_tag_objects,
    _enforce_card_element_limit,
    _escape_md,
    _extract_images_from_markdown,
    _format_elapsed,
    _loading_hint_element,
    _longest_backtick_run,
    _render_footer_field,
    build_preservative_seal_actions,
    build_streaming_card_v2,
    build_unified_panel,
)
from hermes_lark_streaming.cardkit.md import (
    _downgrade_tables,
    _find_tables_outside_code_blocks,
    _split_long_text,
    _strip_invalid_image_keys,
    optimize_markdown_style,
)

import pytest

from hermes_lark_streaming.state.linear import ReasoningRound

# --- Markdown 优化 ---


class TestOptimizeMarkdownStyle:
    def test_h1_downgraded_to_h4(self) -> None:
        assert "#### Title" in optimize_markdown_style("# Title")

    def test_h2_downgraded_to_h5(self) -> None:
        assert "##### Sub" in optimize_markdown_style("## Sub")

    def test_h3_downgraded_to_h5(self) -> None:
        assert "##### Deep" in optimize_markdown_style("### Deep")

    def test_h4_h5_h6_unchanged(self) -> None:
        text = "#### H4\n##### H5\n###### H6"
        result = optimize_markdown_style(text)
        assert "#### H4" in result
        assert "##### H5" in result

    def test_heading_in_code_block_preserved(self) -> None:
        text = "```\n# Should not change\n```"
        assert "# Should not change" in optimize_markdown_style(text)

    def test_blank_line_compression(self) -> None:
        result = optimize_markdown_style("a\n\n\n\n\nb")
        assert "\n\n\n" not in result

    def test_invalid_image_key_removed(self) -> None:
        text = "![alt](not_img_key)"
        assert "not_img_key" not in optimize_markdown_style(text)

    def test_valid_img_key_preserved(self) -> None:
        text = "![alt](img_v3_abc123)"
        assert "img_v3_abc123" in optimize_markdown_style(text)

    def test_no_headings_unchanged(self) -> None:
        text = "plain text\nanother line"
        assert optimize_markdown_style(text) == text

    def test_mixed_headings_and_code(self) -> None:
        text = "# Title\n```\n# Code heading\n```\n## Sub"
        result = optimize_markdown_style(text)
        assert "#### Title" in result
        assert "# Code heading" in result


class TestStripInvalidImageKeys:
    def test_no_images_unchanged(self) -> None:
        assert _strip_invalid_image_keys("no images") == "no images"

    def test_img_prefix_kept(self) -> None:
        assert "img_v3_test" in _strip_invalid_image_keys("![a](img_v3_test)")

    def test_non_img_removed(self) -> None:
        assert "http://example.com/img.png" not in _strip_invalid_image_keys("![a](http://example.com/img.png)")


# --- 表格处理 ---


class TestFindTablesOutsideCodeBlocks:
    def test_no_tables(self) -> None:
        assert _find_tables_outside_code_blocks("no tables here") == []

    def test_single_table(self) -> None:
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        results = _find_tables_outside_code_blocks(text)
        assert len(results) == 1

    def test_table_inside_code_block_ignored(self) -> None:
        text = "```\n| A | B |\n|---|---|\n| 1 | 2 |\n```"
        assert _find_tables_outside_code_blocks(text) == []

    def test_mixed(self) -> None:
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        text = f"{table}\n\n```\n{table}\n```"
        results = _find_tables_outside_code_blocks(text)
        assert len(results) == 1


class TestDowngradeTables:
    def test_within_limit_unchanged(self) -> None:
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        text = f"{table}\n\n{table}\n\n{table}"
        assert _downgrade_tables(text) == text

    def test_over_limit_downgraded(self) -> None:
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        # _MAX_CARD_TABLES = 20, so 22 tables triggers downgrade
        text = "\n\n".join([table] * 22)
        result = _downgrade_tables(text)
        assert result.count("```") >= 4  # 超限表格被包装为代码块


# --- 文本拆分 ---


class TestSplitLongText:
    def test_short_text_not_split(self) -> None:
        assert _split_long_text("short") == ["short"]

    def test_long_text_split_at_paragraph(self) -> None:
        chunk = "x" * 1200
        text = f"{chunk}\n\n{chunk}\n\n{chunk}"
        parts = _split_long_text(text, limit=2000)
        assert len(parts) > 1

    def test_no_paragraph_break_falls_back_to_newline(self) -> None:
        lines = ["word " * 100 for _ in range(30)]
        text = "\n".join(lines)
        parts = _split_long_text(text, limit=500)
        assert len(parts) > 1

    def test_exact_limit_not_split(self) -> None:
        text = "a" * 2400
        assert len(_split_long_text(text)) == 1


# --- 工具面板 ---

_STEP_RUNNING = {
    "name": "read",
    "title": "Read",
    "status": "running",
    "detail": "",
    "output": "",
    "error": "",
    "icon": "icon",
    "elapsed_ms": 0,
    "result_block": None,
    "error_block": None,
}
_STEP_SUCCESS = {**_STEP_RUNNING, "status": "success", "output": "ok", "elapsed_ms": 100}


# --- Footer ---


class TestBuildFooterElements:
    def test_empty_data_renders_default_status(self) -> None:
        # 默认字段包含 "status"，总是会渲染
        result = _build_footer_elements({})
        assert len(result) >= 2
        assert "Completed" in result[1]["content"]

    def test_status_completed(self) -> None:
        result = _build_footer_elements({"duration": 5})
        assert len(result) >= 2  # hr + markdown 元素
        assert "Completed" in result[1]["content"]

    def test_status_error(self) -> None:
        result = _build_footer_elements({}, is_error=True)
        assert "red" in result[1]["content"]

    def test_status_aborted(self) -> None:
        result = _build_footer_elements({}, is_aborted=True)
        assert "Stopped" in result[1]["content"]

    def test_elapsed_displayed(self) -> None:
        result = _build_footer_elements({"duration": 12.5}, fields=[["elapsed"]])
        assert "12.5s" in result[1]["content"]

    def test_model_displayed(self) -> None:
        result = _build_footer_elements({"model": "claude-3"}, fields=[["model"]])
        assert "claude-3" in result[1]["content"]

    def test_context_displayed(self) -> None:
        result = _build_footer_elements(
            {"context_used": 50000, "context_max": 200000},
            fields=[["context"]],
        )
        assert "50.0K" in result[1]["content"]
        assert "25%" in result[1]["content"]

    def test_tokens_displayed(self) -> None:
        result = _build_footer_elements(
            {"input_tokens": 1000, "output_tokens": 500},
            fields=[["tokens"]],
        )
        assert "↑" in result[1]["content"]
        assert "↓" in result[1]["content"]

    def test_show_label(self) -> None:
        result = _build_footer_elements(
            {"duration": 5},
            fields=[["elapsed"]],
            show_label=True,
        )
        assert "Elapsed" in result[1]["content"]

    def test_multi_row_fields(self) -> None:
        result = _build_footer_elements(
            {"duration": 5, "model": "gpt"},
            fields=[["elapsed"], ["model"]],
        )
        assert "\n" in result[1]["content"]

    def test_none_footer_data_renders_status(self) -> None:
        result = _build_footer_elements(None)
        assert len(result) >= 2

    def test_no_matching_fields(self) -> None:
        assert _build_footer_elements({}, fields=[["tokens"]]) == []

    def test_compression_exhausted_displayed(self) -> None:
        result = _build_footer_elements(
            {"compression_exhausted": True},
            fields=[["compression_exhausted"]],
        )
        assert len(result) >= 2
        assert "Context Full" in result[1]["content"]

    def test_api_calls_displayed(self) -> None:
        result = _build_footer_elements(
            {"api_calls": 5},
            fields=[["api_calls"]],
        )
        assert len(result) >= 2
        assert "5" in result[1]["content"]

    def test_history_offset_displayed(self) -> None:
        result = _build_footer_elements(
            {"history_offset": 10},
            fields=[["history_offset"]],
        )
        assert len(result) >= 2
        assert "10" in result[1]["content"]

    def test_cost_estimated_displayed(self) -> None:
        result = _build_footer_elements(
            {"estimated_cost_usd": 0.023, "cost_status": "estimated"},
            fields=[["cost"]],
        )
        assert len(result) >= 2
        assert "$" in result[1]["content"]
        assert "est." in result[1]["content"]

    def test_cost_actual_displayed(self) -> None:
        result = _build_footer_elements(
            {"estimated_cost_usd": 1.50, "cost_status": "actual"},
            fields=[["cost"]],
        )
        assert len(result) >= 2
        assert "$" in result[1]["content"]
        assert "actual" in result[1]["content"]

    def test_cost_included_displayed(self) -> None:
        result = _build_footer_elements(
            {"cost_status": "included"},
            fields=[["cost"]],
        )
        assert len(result) >= 2
        content = result[1]["content"]
        assert "Free" in content or "免费" in content

    def test_cost_unknown_not_displayed(self) -> None:
        result = _build_footer_elements(
            {"cost_status": "unknown"},
            fields=[["cost"]],
        )
        assert result == []

    def test_cost_zero_not_displayed(self) -> None:
        result = _build_footer_elements(
            {"estimated_cost_usd": 0, "cost_status": "estimated"},
            fields=[["cost"]],
        )
        assert result == []

    def test_tokens_with_reasoning_displayed(self) -> None:
        result = _build_footer_elements(
            {"input_tokens": 2100, "output_tokens": 850, "reasoning_tokens": 3200},
            fields=[["tokens"]],
        )
        assert len(result) >= 2
        assert "💭" in result[1]["content"]

    def test_tokens_without_reasoning_no_thinking_icon(self) -> None:
        result = _build_footer_elements(
            {"input_tokens": 2100, "output_tokens": 850},
            fields=[["tokens"]],
        )
        assert len(result) >= 2
        assert "💭" not in result[1]["content"]


# --- 错误面板 ---


class TestBuildErrorPanel:
    def test_error_panel_structure(self) -> None:
        panel = _build_error_panel("something went wrong")
        assert panel["tag"] == "collapsible_panel"
        assert "Error" in panel["header"]["title"]["content"]
        assert panel["border"]["color"] == "red"
        assert panel["expanded"] is True

    def test_aborted_panel_structure(self) -> None:
        panel = _build_error_panel("stopped by user", is_aborted=True)
        assert panel["tag"] == "collapsible_panel"
        assert "Interrupted" in panel["header"]["title"]["content"]
        assert panel["border"]["color"] == "orange"

    def test_error_message_in_elements(self) -> None:
        panel = _build_error_panel("my error detail")
        inner = panel["elements"][0]
        assert inner["tag"] == "markdown"
        assert "my error detail" in inner["content"]

    def test_expanded_default_true(self) -> None:
        panel = _build_error_panel("err")
        assert panel["expanded"] is True

    def test_custom_expanded(self) -> None:
        panel = _build_error_panel("err", expanded=False)
        assert panel["expanded"] is False


# --- 数字格式化 ---


class TestCompact:
    def test_small_number(self) -> None:
        assert _compact(42) == "42"

    def test_thousands(self) -> None:
        assert _compact(1500) == "1.5K"

    def test_millions(self) -> None:
        assert _compact(2_500_000) == "2.5M"

    def test_exact_thousand(self) -> None:
        assert _compact(1000) == "1.0K"

    def test_large_millions(self) -> None:
        assert _compact(250_000_000) == "250M"


class TestFormatElapsed:
    def test_sub_minute(self) -> None:
        assert _format_elapsed(3500) == "3.5s"

    def test_over_minute(self) -> None:
        assert _format_elapsed(125_000) == "2m 5s"

    def test_exactly_one_minute(self) -> None:
        assert _format_elapsed(60_000) == "1m 0s"


# --- 工具函数 ---


class TestEscapeMd:
    def test_escapes_special_chars(self) -> None:
        result = _escape_md("a`b*c{d}e[f]g<h>i")
        assert "\\" in result

    def test_plain_text_unchanged(self) -> None:
        assert _escape_md("hello world") == "hello world"


class TestLongestBacktickRun:
    def test_no_backticks(self) -> None:
        assert _longest_backtick_run("no backticks") == 0

    def test_single(self) -> None:
        assert _longest_backtick_run("a `b` c") == 1

    def test_triple(self) -> None:
        assert _longest_backtick_run("```code```") == 3


# --- 完整卡片构建 ---


class TestBuildStreamingCardV2:
    def test_structure(self) -> None:
        card = build_streaming_card_v2()
        assert card["schema"] == "2.0"
        assert card["config"]["streaming_mode"] is True
        assert card["body"]["elements"]

    def test_with_unified_panel(self) -> None:
        """With include_unified_panel=True (default), unified panel is present."""
        from hermes_lark_streaming.cardkit import UNIFIED_PANEL_ELEMENT_ID
        card = build_streaming_card_v2(tool_steps=[_STEP_RUNNING], elapsed_ms=100)
        assert any(e.get("element_id") == UNIFIED_PANEL_ELEMENT_ID for e in card["body"]["elements"])

    def test_unified_panel_expanded(self) -> None:
        """Unified panel placeholder respects streaming_panel_expanded."""
        from hermes_lark_streaming.cardkit import UNIFIED_PANEL_ELEMENT_ID
        card = build_streaming_card_v2(streaming_panel_expanded=True)
        panel = next(e for e in card["body"]["elements"] if e.get("element_id") == UNIFIED_PANEL_ELEMENT_ID)
        assert panel["expanded"] is True

    def test_unified_panel_collapsed(self) -> None:
        from hermes_lark_streaming.cardkit import UNIFIED_PANEL_ELEMENT_ID
        card = build_streaming_card_v2(streaming_panel_expanded=False)
        panel = next(e for e in card["body"]["elements"] if e.get("element_id") == UNIFIED_PANEL_ELEMENT_ID)
        assert panel["expanded"] is False

    def test_print_strategy_delay(self) -> None:
        card = build_streaming_card_v2(show_reasoning=True)
        assert card["config"]["streaming_config"]["print_strategy"] == "delay"

    def test_print_strategy_fast(self) -> None:
        card = build_streaming_card_v2(show_reasoning=True, print_strategy="fast")
        assert card["config"]["streaming_config"]["print_strategy"] == "fast"

    def test_initial_summary_has_i18n_content(self) -> None:
        """Streaming card must set both content and i18n_content for summary.

        This is critical for Bug #3: Chinese users see i18n_content.zh_cn,
        not content.  If i18n_content is missing, Feishu shows "处理中..."
        even after close_streaming updates content.
        """
        card = build_streaming_card_v2()
        summary = card["config"]["summary"]
        assert "content" in summary
        assert "i18n_content" in summary
        i18n = summary["i18n_content"]
        assert "zh_cn" in i18n
        assert "en_us" in i18n


class TestBuildCronCard:
    def test_basic_card_structure(self) -> None:
        from hermes_lark_streaming.cardkit import build_cron_card

        card = build_cron_card("Hello **world**")
        assert card["schema"] == "2.0"
        assert card["body"]["elements"][0]["tag"] == "markdown"
        assert "Hello **world**" in card["body"]["elements"][0]["content"]

    def test_summary_from_content(self) -> None:
        from hermes_lark_streaming.cardkit import build_cron_card

        card = build_cron_card("Line 1\nLine 2\n" + "x" * 200)
        summary = card["config"]["summary"]["content"]
        assert summary.startswith("Line 1 Line 2")
        assert len(summary) <= 120

    def test_empty_content(self) -> None:
        from hermes_lark_streaming.cardkit import build_cron_card

        card = build_cron_card("")
        assert card["body"]["elements"] == []

    def test_table_content_preserved(self) -> None:
        from hermes_lark_streaming.cardkit import build_cron_card

        content = "| A | B |\n|---|---|\n| 1 | 2 |"
        card = build_cron_card(content)
        assert "| A | B |" in card["body"]["elements"][0]["content"]

    def test_many_tables_triggers_downgrade(self) -> None:
        from hermes_lark_streaming.cardkit import build_cron_card

        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        # _MAX_CARD_TABLES = 20, so 22 tables triggers _downgrade_tables
        content = "\n\n".join([table] * 22)
        card = build_cron_card(content)
        # Excess tables are wrapped in code blocks by _downgrade_tables
        combined = " ".join(e["content"] for e in card["body"]["elements"])
        assert "```" in combined

    def test_long_content_split_into_multiple_elements(self) -> None:
        from hermes_lark_streaming.cardkit import build_cron_card

        # Content longer than 2400 chars should be split by _split_long_text
        content = "x" * 3000
        card = build_cron_card(content)
        assert len(card["body"]["elements"]) > 1

    def test_headings_downgraded_in_cron_card(self) -> None:
        from hermes_lark_streaming.cardkit import build_cron_card

        content = "# Title\nSome text"
        card = build_cron_card(content)
        # optimize_markdown_style downgrades h1 → h4
        combined = " ".join(e["content"] for e in card["body"]["elements"])
        assert "#### Title" in combined


# --- Cache footer field ---


class TestCacheFooterField:
    """_render_footer_field("cache", ...) 缓存命中率字段渲染测试."""

    def test_cache_with_both_tokens(self) -> None:
        """cache_read_tokens + input_tokens 均存在时渲染缓存格式."""
        en, zh = _render_footer_field(
            "cache",
            {"cache_read_tokens": 136300, "input_tokens": 137400},
            is_error=False,
            is_aborted=False,
            show_label=False,
        )
        assert en is not None
        assert zh is not None
        assert "136.3K" in en
        assert "137.4K" in en
        assert "99%" in en

    def test_cache_hit_percentage_calculation(self) -> None:
        """命中率百分比 = cache_read / input_tokens * 100."""
        en, zh = _render_footer_field(
            "cache",
            {"cache_read_tokens": 50000, "input_tokens": 200000},
            is_error=False,
            is_aborted=False,
            show_label=False,
        )
        assert en is not None
        assert "25%" in en
        assert "50.0K" in en
        assert "200.0K" in en

    def test_cache_zero_read_returns_none(self) -> None:
        """cache_read_tokens=0 时返回 (None, None)."""
        en, zh = _render_footer_field(
            "cache",
            {"cache_read_tokens": 0, "input_tokens": 1000},
            is_error=False,
            is_aborted=False,
            show_label=False,
        )
        assert en is None
        assert zh is None

    def test_cache_zero_input_returns_none(self) -> None:
        """input_tokens=0 时返回 (None, None)."""
        en, zh = _render_footer_field(
            "cache",
            {"cache_read_tokens": 500, "input_tokens": 0},
            is_error=False,
            is_aborted=False,
            show_label=False,
        )
        assert en is None
        assert zh is None

    def test_cache_missing_data_returns_none(self) -> None:
        """缺少 cache_read_tokens 或 input_tokens 时返回 (None, None)."""
        en1, zh1 = _render_footer_field(
            "cache", {}, is_error=False, is_aborted=False, show_label=False,
        )
        assert en1 is None

        en2, zh2 = _render_footer_field(
            "cache",
            {"cache_read_tokens": 500},
            is_error=False,
            is_aborted=False,
            show_label=False,
        )
        assert en2 is None

        en3, zh3 = _render_footer_field(
            "cache",
            {"input_tokens": 1000},
            is_error=False,
            is_aborted=False,
            show_label=False,
        )
        assert en3 is None

    def test_cache_show_label_true_english(self) -> None:
        """show_label=True 时英文前缀为 'Cache ...'."""
        en, zh = _render_footer_field(
            "cache",
            {"cache_read_tokens": 1500, "input_tokens": 2000},
            is_error=False,
            is_aborted=False,
            show_label=True,
        )
        assert en is not None
        assert en.startswith("Cache ")
        assert "75%" in en

    def test_cache_show_label_true_chinese(self) -> None:
        """show_label=True 时中文前缀为 '缓存 ...'."""
        en, zh = _render_footer_field(
            "cache",
            {"cache_read_tokens": 1500, "input_tokens": 2000},
            is_error=False,
            is_aborted=False,
            show_label=True,
        )
        assert zh is not None
        assert zh.startswith("缓存 ")
        assert "75%" in zh

    def test_cache_show_label_false_no_prefix(self) -> None:
        """show_label=False 时无标签前缀，直接数字开头."""
        en, zh = _render_footer_field(
            "cache",
            {"cache_read_tokens": 1500, "input_tokens": 2000},
            is_error=False,
            is_aborted=False,
            show_label=False,
        )
        assert en is not None
        assert not en.startswith("Cache")
        assert "75%" in en

    def test_cache_in_build_footer_elements(self) -> None:
        """cache 字段在 _build_footer_elements 中正常渲染."""
        result = _build_footer_elements(
            {"cache_read_tokens": 136300, "input_tokens": 137400},
            fields=[["cache"]],
        )
        assert len(result) >= 2
        assert "99%" in result[1]["content"]

    def test_cache_100_percent(self) -> None:
        """全部命中时显示 100%."""
        en, zh = _render_footer_field(
            "cache",
            {"cache_read_tokens": 1000, "input_tokens": 1000},
            is_error=False,
            is_aborted=False,
            show_label=False,
        )
        assert en is not None
        assert "100%" in en


# --- 图片提取 ---


class TestExtractImagesFromMarkdown:
    """_extract_images_from_markdown() 图片提取测试."""

    def test_no_images(self) -> None:
        """无图片时返回原文本和空列表."""
        text = "Hello world, no images here."
        cleaned, images = _extract_images_from_markdown(text)
        assert cleaned == text
        assert images == []

    def test_single_img_key(self) -> None:
        """提取单个飞书 img_key 图片."""
        text = "Here is an image:\n![alt text](img_v3_0238_abc123)\nMore text."
        cleaned, images = _extract_images_from_markdown(text)
        assert "img_v3_0238_abc123" not in cleaned
        assert "More text" in cleaned
        assert len(images) == 1
        assert images[0]["tag"] == "img"
        assert images[0]["img_key"] == "img_v3_0238_abc123"
        assert images[0]["scale_type"] == "fit_horizontal"
        assert images[0]["alt"]["content"] == "alt text"
        assert images[0]["preview"] is True
        assert images[0]["corner_radius"] == "8px"

    def test_multiple_images(self) -> None:
        """提取多个飞书图片."""
        text = "![a](img_v3_001) text ![b](img_v3_002)"
        cleaned, images = _extract_images_from_markdown(text)
        assert len(images) == 2
        assert images[0]["img_key"] == "img_v3_001"
        assert images[1]["img_key"] == "img_v3_002"
        assert "img_v3" not in cleaned

    def test_non_img_key_preserved(self) -> None:
        """非 img_ 前缀的 URL 不被提取（由 _strip_invalid_image_keys 处理）."""
        text = "![alt](https://example.com/image.png)"
        cleaned, images = _extract_images_from_markdown(text)
        assert len(images) == 0
        # 非img_key的保留原样（后续由 _strip_invalid_image_keys 移除）
        assert "https://example.com/image.png" in cleaned

    def test_mixed_images(self) -> None:
        """混合 img_key 和外部 URL：只提取 img_key."""
        text = "![good](img_v3_abc) ![bad](https://example.com/img.png)"
        cleaned, images = _extract_images_from_markdown(text)
        assert len(images) == 1
        assert images[0]["img_key"] == "img_v3_abc"
        assert "https://example.com/img.png" in cleaned

    def test_empty_alt(self) -> None:
        """空 alt 文本的图片也能提取."""
        text = "![](img_v3_empty_alt)"
        cleaned, images = _extract_images_from_markdown(text)
        assert len(images) == 1
        assert images[0]["alt"]["content"] == ""

    def test_cleans_extra_newlines(self) -> None:
        """图片移除后清理多余空行."""
        text = "Before\n\n![img](img_v3_xxx)\n\nAfter"
        cleaned, images = _extract_images_from_markdown(text)
        assert len(images) == 1
        # 不应出现3个以上连续换行
        assert "\n\n\n" not in cleaned
        assert "Before" in cleaned
        assert "After" in cleaned



class TestBackgroundReviewPanel:
    """后台审查面板测试."""

    def test_build_background_review_panel(self) -> None:
        """构建后台审查面板."""
        from hermes_lark_streaming.cardkit import _build_background_review_panel
        panel = _build_background_review_panel(["检查完成", "更新记忆"])
        assert panel["tag"] == "collapsible_panel"
        assert len(panel["elements"]) == 2

    def test_build_background_review_panel_empty(self) -> None:
        """空消息列表的面板."""
        from hermes_lark_streaming.cardkit import _build_background_review_panel
        panel = _build_background_review_panel([])
        assert panel["tag"] == "collapsible_panel"
        assert len(panel["elements"]) == 1  # placeholder

class TestLoadingHintElement:
    """_loading_hint_element() 占位元素结构测试."""

    def test_element_id(self) -> None:
        el = _loading_hint_element()
        assert el["element_id"] == _LOADING_HINT_ELEMENT_ID

    def test_tag_is_div(self) -> None:
        el = _loading_hint_element()
        assert el["tag"] == "div"

    def test_icon_is_standard_icon(self) -> None:
        el = _loading_hint_element()
        assert el["icon"]["tag"] == "standard_icon"
        assert el["icon"]["token"] == "time_outlined"

    def test_text_is_lark_md(self) -> None:
        el = _loading_hint_element()
        assert el["text"]["tag"] == "lark_md"

    def test_text_has_i18n(self) -> None:
        el = _loading_hint_element()
        assert "i18n_content" in el["text"]

    def test_streaming_card_does_not_include_hint(self) -> None:
        """流式占位卡片默认包含占位提示（v1.0.2: 嵌入初始卡片，不再单独API插入）."""
        card = build_streaming_card_v2()
        element_ids = [e.get("element_id") for e in card["body"]["elements"]]
        assert _LOADING_HINT_ELEMENT_ID in element_ids  # v1.0.2: hint is now pre-embedded

    def test_streaming_card_without_loading_hint(self) -> None:
        """include_loading_hint=False 时占位卡片不包含占位提示."""
        card = build_streaming_card_v2(include_loading_hint=False)
        element_ids = [e.get("element_id") for e in card["body"]["elements"]]
        assert _LOADING_HINT_ELEMENT_ID not in element_ids


class TestPreservativeSealActionsDeleteHint:
    """build_preservative_seal_actions() 包含删除占位提示的 action."""

    def test_partial_seal_deletes_loading_hint(self) -> None:
        actions = build_preservative_seal_actions(partial=True)
        delete_actions = [
            a for a in actions
            if a["action"] == "delete_elements"
            and a["params"]["element_ids"] == [_LOADING_HINT_ELEMENT_ID]
        ]
        assert len(delete_actions) == 1

    def test_full_seal_deletes_loading_hint(self) -> None:
        actions = build_preservative_seal_actions(
            footer_data={},
            footer_fields=[["status"]],
        )
        delete_actions = [
            a for a in actions
            if a["action"] == "delete_elements"
            and a["params"]["element_ids"] == [_LOADING_HINT_ELEMENT_ID]
        ]
        assert len(delete_actions) == 1

    def test_delete_hint_before_delete_loading_icon(self) -> None:
        """占位提示删除在 loading icon 删除之前（顺序重要）."""
        actions = build_preservative_seal_actions(partial=True)
        delete_ids = [
            id
            for a in actions
            if a["action"] == "delete_elements"
            for id in a["params"]["element_ids"]
        ]
        assert _LOADING_HINT_ELEMENT_ID in delete_ids
        assert "loading_icon" in delete_ids
        hint_idx = delete_ids.index(_LOADING_HINT_ELEMENT_ID)
        loading_idx = delete_ids.index("loading_icon")
        assert hint_idx < loading_idx


# --- Bug #3 regression: summary i18n_content ---


class TestSummaryI18nContent:
    """Bug #3: close_streaming must update i18n_content, not just content.

    Feishu CardKit 2.0 displays i18n_content.<locale> based on the user's
    language preference.  For Chinese users, Feishu shows zh_cn.  If we
    only update "content" but not "i18n_content" when closing streaming,
    the conversation list continues showing "处理中..." forever — even
    though close_streaming succeeded and "content" was updated.
    """

    def test_streaming_card_v2_summary_has_i18n(self) -> None:
        card = build_streaming_card_v2()
        summary = card["config"]["summary"]
        assert "i18n_content" in summary
        assert "zh_cn" in summary["i18n_content"]
        assert "en_us" in summary["i18n_content"]


class TestBuildUnifiedPanelTrimming:
    """Tests for element limit trimming in build_unified_panel."""

    def _make_rounds(self, n: int) -> list:
        """Create n reasoning rounds."""
        from hermes_lark_streaming.state.linear import ReasoningRound
        rounds = [ReasoningRound(index=i + 1, text=f"Reasoning {i + 1}") for i in range(n)]
        for r in rounds:
            r.elapsed_ms = 100
        return rounds

    def _make_steps(self, n: int) -> list[dict]:
        """Create n tool steps."""
        return [
            {"name": f"tool_{i}", "status": "success", "title": f"Tool {i}",
             "detail": f"Detail {i}", "result_block": {"content": f"Result {i}", "language": "text"}}
            for i in range(n)
        ]

    def test_trim_reasoning_rounds(self):
        """Excess reasoning rounds are trimmed with collapse hint."""
        rounds = self._make_rounds(30)
        steps = self._make_steps(5)
        panel = build_unified_panel(
            reasoning_rounds=rounds,
            tool_steps=steps,
            show_reasoning=True,
            max_reasoning_rounds=20,
            max_tool_steps=20,
        )
        # Panel title should show original count (30 rounds)
        title_content = panel["header"]["title"]["content"]
        assert "30" in title_content
        # Collapse hint should be first child
        children = panel["elements"]
        first = children[0]
        assert first["tag"] == "markdown"
        assert "10 轮早期推理" in first["content"]
        assert "已折叠" in first["content"]

    def test_trim_tool_steps(self):
        """Excess tool steps are trimmed with collapse hint."""
        rounds = self._make_rounds(5)
        steps = self._make_steps(30)
        panel = build_unified_panel(
            reasoning_rounds=rounds,
            tool_steps=steps,
            show_reasoning=True,
            max_reasoning_rounds=20,
            max_tool_steps=20,
        )
        # Panel title should show original count (30 tools)
        title_content = panel["header"]["title"]["content"]
        assert "30" in title_content
        # Collapse hint should mention trimmed tools
        children = panel["elements"]
        first = children[0]
        assert first["tag"] == "markdown"
        assert "10 步早期操作" in first["content"]
        assert "已折叠" in first["content"]

    def test_trim_both(self):
        """Both reasoning and tools are trimmed when both exceed limits."""
        rounds = self._make_rounds(30)
        steps = self._make_steps(30)
        panel = build_unified_panel(
            reasoning_rounds=rounds,
            tool_steps=steps,
            show_reasoning=True,
            max_reasoning_rounds=20,
            max_tool_steps=20,
        )
        children = panel["elements"]
        first = children[0]
        assert "10 轮早期推理" in first["content"]
        assert "10 步早期操作" in first["content"]

    def test_no_trim_when_within_limit(self):
        """No trimming when counts are within limits."""
        rounds = self._make_rounds(10)
        steps = self._make_steps(10)
        panel = build_unified_panel(
            reasoning_rounds=rounds,
            tool_steps=steps,
            show_reasoning=True,
            max_reasoning_rounds=20,
            max_tool_steps=20,
        )
        children = panel["elements"]
        # No collapse hint should be present
        for child in children:
            if child.get("tag") == "markdown" and "已折叠" in child.get("content", ""):
                pytest.fail("Unexpected collapse hint when within limits")

    def test_panel_events_filtered_after_trim(self):
        """panel_events are correctly reindexed after trimming."""
        rounds = self._make_rounds(25)
        steps = self._make_steps(25)
        panel_events = [(f"reasoning", i) for i in range(25)] + [(f"tool", i) for i in range(25)]
        panel = build_unified_panel(
            reasoning_rounds=rounds,
            tool_steps=steps,
            show_reasoning=True,
            panel_events=panel_events,
            max_reasoning_rounds=20,
            max_tool_steps=20,
        )
        # Should have a collapse hint and remaining items
        assert panel["elements"][0]["tag"] == "markdown"
        assert "已折叠" in panel["elements"][0]["content"]

    def test_custom_limits(self):
        """Custom max values work correctly."""
        rounds = self._make_rounds(10)
        steps = self._make_steps(10)
        panel = build_unified_panel(
            reasoning_rounds=rounds,
            tool_steps=steps,
            show_reasoning=True,
            max_reasoning_rounds=5,
            max_tool_steps=5,
        )
        children = panel["elements"]
        first = children[0]
        assert "5 轮早期推理" in first["content"]
        assert "5 步早期操作" in first["content"]

    def test_safety_net_trims_worst_case(self):
        """Card-level safety net kicks in when total elements exceed 195.

        With 20 reasoning rounds + 20 tool steps (each with max elements),
        the total card element count can exceed 200. The card-level safety
        net in _enforce_card_element_limit trims panel children until
        the total is at or below 195.
        """
        # 20 rounds + 20 steps, each with max elements (detail + result for tools)
        from hermes_lark_streaming.cardkit.elements import build_unified_panel as _build_panel
        rounds = self._make_rounds(20)
        steps = self._make_steps(20)
        panel = _build_panel(
            reasoning_rounds=rounds,
            tool_steps=steps,
            show_reasoning=True,
            max_reasoning_rounds=20,
            max_tool_steps=20,
        )
        card = {
            "schema": "2.0",
            "body": {"elements": [panel, {"tag": "markdown", "content": "Test answer"}]},
        }
        result = _enforce_card_element_limit(card)
        total_count = _count_tag_objects(result)
        # Should be at or below 195 (200 - 5 margin)
        assert total_count <= 195, f"Total element count {total_count} exceeds safety threshold 195"
        # Should have a collapse hint in panel children (safety net trimmed items)
        panel = None
        for elem in result.get("body", {}).get("elements", []):
            if elem.get("element_id") == "agent_process_panel":
                panel = elem
                break
        assert panel is not None
        first_child = panel["elements"][0]
        assert "已折叠" in first_child.get("content", "")


class TestEnforceCardElementLimit:
    """Tests for the card-level element limit safety net."""

    def _make_rounds(self, n: int) -> list:
        """Create n reasoning rounds."""
        from hermes_lark_streaming.state.linear import ReasoningRound
        rounds = [ReasoningRound(index=i + 1, text=f"Reasoning {i + 1}") for i in range(n)]
        for r in rounds:
            r.elapsed_ms = 100
        return rounds

    def _make_steps(self, n: int) -> list[dict]:
        """Create n tool steps."""
        return [
            {"name": f"tool_{i}", "status": "success", "title": f"Tool {i}",
             "detail": f"Detail {i}", "result_block": {"content": f"Result {i}", "language": "text"}}
            for i in range(n)
        ]

    def test_no_trim_when_under_limit(self):
        """Card under 195 elements is not trimmed."""
        from hermes_lark_streaming.cardkit.elements import build_unified_panel as _build_panel
        panel = _build_panel(
            reasoning_rounds=self._make_rounds(5),
            tool_steps=self._make_steps(5),
            show_reasoning=True,
        )
        card = {
            "schema": "2.0",
            "body": {"elements": [panel, {"tag": "markdown", "content": "Short answer"}]},
        }
        result = _enforce_card_element_limit(card)
        total = _count_tag_objects(result)
        assert total <= 195
        # No collapse hint should exist
        panel = None
        for elem in result.get("body", {}).get("elements", []):
            if elem.get("element_id") == "agent_process_panel":
                panel = elem
                break
        if panel:
            for child in panel.get("elements", []):
                if "已折叠" in child.get("content", ""):
                    pytest.fail("Unexpected collapse hint when under limit")

    def test_trim_preserves_answer_and_footer(self):
        """Answer text and footer are never trimmed — only panel children."""
        from hermes_lark_streaming.cardkit.elements import build_unified_panel as _build_panel
        panel = _build_panel(
            reasoning_rounds=self._make_rounds(25),
            tool_steps=self._make_steps(25),
            show_reasoning=True,
            max_reasoning_rounds=25,
            max_tool_steps=25,
        )
        card = {
            "schema": "2.0",
            "body": {"elements": [
                panel,
                {"tag": "markdown", "content": "This answer must not be trimmed"},
                {"tag": "hr"},
                {"tag": "markdown", "content": "footer"},
            ]},
        }
        result = _enforce_card_element_limit(card)
        total = _count_tag_objects(result)
        assert total <= 195
        # Answer must still be present
        body_elements = result.get("body", {}).get("elements", [])
        answer_found = any(
            "This answer must not be trimmed" in elem.get("content", "")
            for elem in body_elements
            if elem.get("tag") == "markdown"
        )
        assert answer_found, "Answer text was trimmed — safety net should only trim panel children"
        # Footer must still be present
        footer_found = any(elem.get("tag") == "hr" for elem in body_elements)
        assert footer_found, "Footer was trimmed — safety net should only trim panel children"

    def test_enforce_card_element_limit_directly(self):
        """_enforce_card_element_limit trims a card dict directly."""
        # Build a card that would exceed 195 elements
        from hermes_lark_streaming.cardkit.elements import build_unified_panel as _build_panel
        from hermes_lark_streaming.cardkit.i18n import _LOCALES

        panel = _build_panel(
            reasoning_rounds=self._make_rounds(25),
            tool_steps=self._make_steps(25),
            show_reasoning=True,
            max_reasoning_rounds=25,
            max_tool_steps=25,
        )
        elements = [panel, {"tag": "markdown", "content": "Test answer"}, {"tag": "hr"}, {"tag": "markdown", "content": "footer"}]
        card = {
            "schema": "2.0",
            "body": {"elements": elements},
        }
        pre_count = _count_tag_objects(card)
        result = _enforce_card_element_limit(card)
        post_count = _count_tag_objects(result)
        assert post_count <= 195, f"Post-enforce count {post_count} exceeds 195"
        assert post_count < pre_count, "Element count should have decreased after enforcement"

    def test_enforce_does_nothing_when_under_limit(self):
        """_enforce_card_element_limit is a no-op when under 195."""
        card = {
            "schema": "2.0",
            "config": {"streaming_mode": False},
            "body": {"elements": [
                {"tag": "markdown", "content": "Hello"},
                {"tag": "hr"},
                {"tag": "markdown", "content": "footer"},
            ]},
        }
        pre_count = _count_tag_objects(card)
        result = _enforce_card_element_limit(card)
        post_count = _count_tag_objects(result)
        assert pre_count == post_count

    def test_no_panel_no_crash(self):
        """_enforce_card_element_limit handles cards without a panel."""
        card = {
            "schema": "2.0",
            "config": {"streaming_mode": False},
            "body": {"elements": [
                {"tag": "markdown", "content": "Hello"},
            ]},
        }
        result = _enforce_card_element_limit(card)
        assert result is card  # Unchanged
