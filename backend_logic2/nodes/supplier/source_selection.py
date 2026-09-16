"""Policy-selected candidate discovery; enrichment stays in the existing path."""
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor

LOGGER = logging.getLogger(__name__)
SOURCE_PRIORITY = ('narajangteo', 'db', 'tavily')


def search_cache_key(normalized, sources):
    # Do not reuse historical mixed-source cache when a source is deselected.
    # Fixed-size key also fits legacy varchar columns.
    identity = f"{normalized}\n{','.join(sorted(sources))}"
    return 'sources-v1:' + hashlib.sha256(identity.encode('utf-8')).hexdigest()


def collect_selected(item_name, normalized, target, sources, case_id=None):
    from .tools.narajangteo_search_based_tool import search_all, search_db_cache
    from .tools.structured_item_search_tool import collect_candidate_names_structured
    calls = {
        'narajangteo': lambda: search_all(normalized, target_count=target),
        'db': lambda: search_db_cache(normalized, target_count=target),
        'tavily': lambda: collect_candidate_names_structured(item_name, target, case_id=case_id),
    }
    if not sources or set(sources) - set(calls):
        raise ValueError('At least one supported supplier source is required')
    # Merge in a fixed priority order, not whichever thread finishes first.
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        tasks = {source: pool.submit(calls[source]) for source in SOURCE_PRIORITY if source in sources}
        results = []
        for source, future in tasks.items():
            try:
                results.extend(future.result())
            except Exception:
                LOGGER.warning('Supplier candidate source failed: %s', source)
    return results
