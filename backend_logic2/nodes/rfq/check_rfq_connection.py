"""
nodes/rfq/check_rfq_connection.py

"Request for Quotation" 목록 조회 API가 실제로 어떤 응답을 주는지
raw하게 확인하는 진단용 스크립트. remind_rfq.py에서 JSON 파싱 에러가 나서
원인을 못 찾을 때, 이걸 먼저 실행해서 상태코드/본문을 직접 확인할 것.

실행: python -m backend_logic2.nodes.rfq.check_rfq_connection
"""

import requests

from backend_logic2.integrations.erp_client import API_KEY, API_SECRET, HEADERS, SITE_URL


def main():
    print(f"SITE_URL = {SITE_URL!r}")
    print(f"API_KEY  = {API_KEY[:4]}...(생략)")
    print()

    url = f"{SITE_URL}/api/resource/Request for Quotation"
    print(f"GET {url}")
    res = requests.get(url, headers=HEADERS, params={"limit_page_length": 1})

    print(f"status_code = {res.status_code}")
    print(f"final url   = {res.url}")
    print(f"content-type = {res.headers.get('Content-Type')}")
    print("--- 본문 앞 500자 ---")
    print(res.text[:500])
    print("---------------------")

    if res.status_code == 200 and "application/json" not in (res.headers.get("Content-Type") or ""):
        print(
            "\n[진단] 상태코드는 200인데 JSON이 아닙니다. 대부분 아래 중 하나입니다:\n"
            "  1) API 키/시크릿이 만료됐거나 잘못돼서 로그인 페이지(HTML)로 리다이렉트됨\n"
            "  2) SITE_URL이 실제 ERPNext 서버 주소가 아님 (오타, 포트 누락, http/https 등)\n"
            "  3) 이 API 사용자에게 'Request for Quotation' 문서 Read 권한이 없음\n"
            "  4) 사내망/VPN 프록시가 요청을 가로채서 안내 페이지를 대신 돌려줌\n"
        )
    elif res.status_code != 200:
        print(f"\n[진단] status_code={res.status_code}. 본문 내용을 보고 권한/필드 문제인지 확인하세요.")
    else:
        print("\n[진단] 정상적으로 JSON을 받았습니다. remind_rfq.py 쪽 문제는 아닌 것으로 보입니다.")


if __name__ == "__main__":
    main()
