from itertools import combinations
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from backend_logic2.policies.schema import CompanyPolicy, PublishPolicy
from backend_logic2.policies.runtime import policy_scope
from backend_logic2.nodes.supplier.source_selection import collect_selected, search_cache_key


def test_default_preserves_tavily_and_empty_invalid():
    assert CompanyPolicy().supplier_sources == ['tavily']
    for value in [[], ['unknown'], ['db', 'db']]:
        with pytest.raises(ValidationError):
            CompanyPolicy(supplier_sources=value)
    old = CompanyPolicy().model_dump(); old.pop('supplier_sources')
    assert CompanyPolicy.model_validate(old).supplier_sources == ['tavily']
    with pytest.raises(ValidationError):
        PublishPolicy(expected_version=1, policy=old, reason='legacy client must reload')


def test_all_seven_combinations_call_only_selected_sources():
    nara = 'backend_logic2.nodes.supplier.tools.narajangteo_search_based_tool'
    tavily = 'backend_logic2.nodes.supplier.tools.structured_item_search_tool'
    for count in (1, 2, 3):
        for sources in combinations(['tavily', 'narajangteo', 'db'], count):
            with patch(nara + '.search_all', return_value=[{'name': '나라'}]) as a, \
                 patch(nara + '.search_db_cache', return_value=[{'name': 'DB'}]) as b, \
                 patch(tavily + '.collect_candidate_names_structured', return_value=[{'name': '웹'}]) as c:
                results = collect_selected('원본', '정규화', 10, sources)
                assert a.call_count == int('narajangteo' in sources)
                assert b.call_count == int('db' in sources)
                assert c.call_count == int('tavily' in sources)
                assert len(results) == count


def test_cache_is_partitioned_by_sources_and_order_independent():
    assert search_cache_key('볼트', ['db']) != search_cache_key('볼트', ['tavily'])
    assert search_cache_key('볼트', ['db', 'tavily']) == search_cache_key('볼트', ['tavily', 'db'])


def test_real_search_uses_pinned_policy_and_cache_partition():
    from backend_logic2.nodes.supplier import supplier_search as search
    with patch.object(search, 'normalize_item_name', return_value='볼트'), \
         patch.object(search, 'cleanup_expired_cache'), \
         patch.object(search, 'get_cached_results', return_value=[]) as cache, \
         patch.object(search, 'collect_selected', return_value=[] ) as collect, \
         patch.object(search, 'dart_verify_and_fetch_contacts', return_value=([], [])), \
         patch.object(search, 'save_to_cache') as save, \
         patch.object(search, 'log_status_change'):
        with policy_scope(CompanyPolicy(supplier_sources=['db'])):
            assert search.supplier_search('테스트 볼트') == []
        assert collect.call_args.args[3] == ['db']
        assert cache.call_args.args[0] == save.call_args.args[0] == search_cache_key('볼트', ['db'])
