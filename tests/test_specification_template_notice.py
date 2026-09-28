from unittest.mock import patch

from backend_logic2.integrations.specification_text import remove_item_template_notice
from backend_logic2.nodes.quotation.quotation_filter.quotation_reviewer import extract_specifications
from backend_logic2.nodes.item import item_spec_validation as validation

NOTICE = '[자동생성 - 품목분류 필수 규격, 값을 채워주세요]'


def test_strip_only_notice_preserving_required_labels_and_values():
    assert remove_item_template_notice(NOTICE + '\n재질: SUS304\n두께: ') == '재질: SUS304\n두께:'
    assert remove_item_template_notice('[필수 규격] 전압: 220V') == '[필수 규격] 전압: 220V'


def test_quotation_requirement_does_not_inherit_notice_as_a_label():
    actual = extract_specifications(f'<p>{NOTICE}색상: 흰색</p>')
    assert actual['color'] == '흰색'
    assert not any('자동생성' in key for key in actual)


def test_notice_alone_never_counts_as_item_description():
    with patch.object(validation, 'get_or_create_group_requirements', return_value={
        'required_specs': ['재질'], 'reason': 'test',
    }), patch.object(validation, '_ai_check_completeness') as model:
        result = validation.check_item_spec_completeness('test', NOTICE)
    assert result['complete'] is False
    assert result['missing'] == ['재질']
    model.assert_not_called()


def test_item_completeness_model_receives_actual_specs_only():
    with patch.object(validation, 'get_or_create_group_requirements', return_value={
        'required_specs': ['재질'], 'reason': 'test',
    }), patch.object(validation, 'log_ai_decision'), patch.object(
        validation, '_ai_check_completeness', return_value=[
            {'spec': '재질', 'present': True, 'reason': 'specified'},
        ],
    ) as model:
        validation.check_item_spec_completeness('test', NOTICE + '\n재질: SUS304, 두께: 2mm')
    assert model.call_args.args[1] == '재질: SUS304, 두께: 2mm'


def test_substitute_comparison_omits_entry_notice():
    from backend_logic2.nodes.mr.find_substitute import _strip_html
    assert _strip_html(f'<p>{NOTICE}</p><p>전압: 220V</p>') == '전압: 220V'
