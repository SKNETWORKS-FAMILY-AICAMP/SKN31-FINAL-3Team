"""Regression: passing the policy gate must carry its winner into selection.

No DB, ERP writes or email: execute the node handoffs with external calls mocked.
"""
from unittest.mock import patch
import pytest
from backend_logic2.services.auto_progress import AutoDecision
from backend_logic2.workflow.process_commands import (
    check_quotations_command, final_selection_command,
    await_order_start_command, request_pr_command,
)


def run_check(mode='on', ranking=None, allowed=True, enabled=True):
    rows = ranking if ranking is not None else [
        {'supplier': 'winner', 'quotation_id': 'SQ-WIN', 'rfq_name': 'RFQ-OLD'},
        {'supplier': 'second', 'quotation_id': 'SQ-2', 'rfq_name': 'RFQ-NEW'},
    ]
    with patch('backend_logic2.workflow.process_commands.interrupt', return_value={
        'decision': 'auto', 'ready': True, 'supplier': 'untrusted-input', 'quotation_id': 'wrong',
    }), patch('backend_logic2.workflow.process_commands._judge_final_selection',
              return_value=AutoDecision(allowed=allowed, mode=mode, enabled=enabled)), \
         patch('backend_logic2.nodes.quotation.quotation_filter.quotation_ranker.evaluate_quotations_for_rfqs',
               return_value={'quotations': [{}], 'ranking': rows}), \
         patch('backend_logic2.nodes.quotation.quotation_filter.quotation_ranker.print_evaluation'), \
         patch('backend_logic2.nodes.quotation.quotation_filter.quotation_registrar.submit_finalized_quotations') as submit:
        command = check_quotations_command({'rfq_name': 'RFQ-NEW'})
    return command, submit


def test_approved_auto_winner_reaches_pr_creation_without_extra_interrupt():
    command, submit = run_check()
    assert command.goto == 'final_selection'
    assert command.update['requested_supplier'] == 'winner'
    assert command.update['requested_quotation'] == 'SQ-WIN'
    assert command.update['selection_mode'] == 'auto'
    submit.assert_called_once()
    state = {'mr_name': 'MR-TEST', 'rfq_name': 'RFQ-NEW', **command.update}
    with patch('backend_logic2.workflow.process_commands.interrupt', side_effect=AssertionError('unexpected human wait')):
        selected = final_selection_command(state)
        state.update(selected.update)
        assert selected.goto == 'await_order_start'
        assert state['selected_quotation'] == 'SQ-WIN'
        assert state['selected_rfq_name'] == 'RFQ-OLD'
        order = await_order_start_command(state)
        state.update(order.update)
        assert order.goto == 'request_pr'
        pr = request_pr_command(state)
        assert pr.goto == 'create_pr'
        assert pr.update['auto_pr_dispatch'] is False


@pytest.mark.parametrize('mode,allowed,enabled', [('off', True, True), ('shadow', True, True), ('on', False, True), ('on', True, False)])
def test_non_executing_decisions_do_not_submit_or_select(mode, allowed, enabled):
    command, submit = run_check(mode=mode, allowed=allowed, enabled=enabled)
    assert command.goto == 'check_quotations'
    assert not command.update.get('requested_supplier')
    submit.assert_not_called()


@pytest.mark.parametrize('row', [{'supplier': 'winner'}, {'quotation_id': 'SQ-WIN'}])
def test_incomplete_winner_fails_closed(row):
    command, submit = run_check(ranking=[row])
    assert command.goto == 'check_quotations'
    assert command.update['auto_pr_dispatch'] is False
    submit.assert_not_called()
