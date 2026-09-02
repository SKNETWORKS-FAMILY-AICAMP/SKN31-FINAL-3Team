"""
create_mr_substitute_client_script.py — Material Request 화면에 "AI 대체품
확인" 버튼을 붙이는 ERPNext Client Script를 REST API로 등록/갱신함
(2026-09-01). ERPNext 관리화면을 직접 안 만져도 됨 - Client Script도
그냥 DocType이라 우리가 지금까지 해온 것처럼 REST API로 밀어넣으면 됨.

실행 전 .env에 아래 2개를 추가로 넣어야 함:
  PROCUREMENT_API_BASE_URL - 이 FastAPI 서버(main.py)가 ERPNext 쪽에서
    실제로 네트워크로 접근 가능한 주소(예: https://your-domain:8000).
    로컬(localhost)에서만 떠있으면 ERPNext 서버가 못 불러서 버튼이 항상
    실패함 - 반드시 ERPNext가 도달 가능한 주소여야 함(이건 코드로 해결
    안 되는 배포/네트워크 문제라 별도로 처리 필요).
  CLIENT_SCRIPT_SECRET - backend_logic2/api/mr_substitute_routes.py가
    요구하는 것과 완전히 같은 값. 여기서 Client Script JS 코드 안에
    그대로 박아넣음(ERPNext 화면 접속 가능한 사람은 브라우저 페이지
    소스로 이 값을 볼 수 있음 - erp_client.py가 이미 공용 API키 하나로
    돌아가는 것과 같은 수준의 신뢰모델이라 지금 단계에선 허용).

여러 번 실행해도 안전 - 고정된 이름(CLIENT_SCRIPT_NAME)의 기존 Client
Script를 지우고 다시 만듦(idempotent).

사용법 (레포 루트에서):
    python create_mr_substitute_client_script.py
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

from backend_logic2.integrations.erp_client import SITE_URL, HEADERS, ERPNextAPIError

API_BASE = os.environ.get("PROCUREMENT_API_BASE_URL", "").rstrip("/")
SECRET = os.environ.get("CLIENT_SCRIPT_SECRET", "")

if not API_BASE or not SECRET:
    raise SystemExit("PROCUREMENT_API_BASE_URL / CLIENT_SCRIPT_SECRET을 .env에 먼저 설정하세요.")

# 이 마커로 "우리가 등록한 Client Script"를 찾아서 재실행 시 지우고 다시 만듦.
MARKER = "// PROCUREMENT_AI_SUBSTITUTE_BUTTON_V1"

SCRIPT_JS = f"""{MARKER}
frappe.ui.form.on('Material Request', {{
    refresh: function(frm) {{
        // 2026-09-01: "Draft-first" 구조로 바뀌면서 대체품 선택은 이제
        // MR이 Submit되기 전(Draft)에 일어남 - Submit 자체가 "대체품
        // 미사용 확정"의 결과이지, 대체품 선택의 전제조건이 아님. 그래서
        // 기존 docstatus===1(Submit됨) 조건을 뒤집어서 Draft에서만 버튼이
        // 뜨게 함.
        if (frm.doc.docstatus !== 0) return;

        frm.add_custom_button('AI 대체품 확인', function() {{
            fetch('{API_BASE}/api/mr/' + frm.doc.name + '/substitutes', {{
                headers: {{'X-Client-Script-Secret': '{SECRET}'}}
            }})
            .then(r => r.json())
            .then(data => {{
                if (!data.awaiting || !data.candidates || !data.candidates.length) {{
                    frappe.msgprint('지금 대체품 확인이 필요한 상태가 아닙니다.');
                    return;
                }}

                let fields = data.candidates.map(function(c, i) {{
                    let fulfill = c.fulfills_full_qty ? '전량충족' : '부분충족';
                    return {{
                        fieldtype: 'Button',
                        fieldname: 'choice_' + i,
                        label: (i + 1) + '. ' + c.item_name + ' (재고 ' + c.total_qty + ', ' + fulfill + ') - ' + (c.reason || ''),
                        click: function() {{
                            submit_substitute_decision(frm, {{item_code: c.item_code}});
                            d.hide();
                        }}
                    }};
                }});
                fields.push({{
                    fieldtype: 'Button',
                    fieldname: 'buy_original',
                    label: '원래 품목 그대로 구매',
                    click: function() {{
                        submit_substitute_decision(frm, {{decision: 'new_purchase'}});
                        d.hide();
                    }}
                }});

                var d = new frappe.ui.Dialog({{title: '대체품 추천', fields: fields}});
                d.show();
            }})
            .catch(function(err) {{
                frappe.msgprint('대체품 후보 조회 실패: ' + err);
            }});
        }});
    }}
}});

function submit_substitute_decision(frm, payload) {{
    fetch('{API_BASE}/api/mr/' + frm.doc.name + '/substitute-decision', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json', 'X-Client-Script-Secret': '{SECRET}'}},
        body: JSON.stringify(payload)
    }})
    .then(r => r.json())
    .then(function(res) {{
        if (res.success) {{
            frappe.msgprint('처리되었습니다.');
        }} else {{
            frappe.msgprint('다시 선택해주세요: ' + (res.error || '알 수 없는 오류'));
        }}
        frm.reload_doc();
    }})
    .catch(function(err) {{
        frappe.msgprint('처리 실패: ' + err);
    }});
}}
"""


# Client Script는 autoname이 "Prompt"라 name을 직접 지정해줘야 함(안 주면
# ERPNext가 "Please set the document name" 에러를 던짐 - 실제로 겪음).
# 공백 없이 고정된 이름으로 둬서 재실행할 때 URL 경로에 그대로 안전하게 씀.
CLIENT_SCRIPT_NAME = "Procurement_AI_Substitute_Button"


def main():
    existing = requests.get(f"{SITE_URL}/api/resource/Client Script/{CLIENT_SCRIPT_NAME}", headers=HEADERS)
    if existing.status_code == 200:
        res = requests.delete(f"{SITE_URL}/api/resource/Client Script/{CLIENT_SCRIPT_NAME}", headers=HEADERS)
        if res.status_code not in (200, 202):
            print(f"  경고: 기존 Client Script 삭제 실패: {res.status_code} - {res.text[:200]}")
        else:
            print(f"  기존 Client Script 삭제: {CLIENT_SCRIPT_NAME}")

    payload = {
        "name": CLIENT_SCRIPT_NAME,
        "dt": "Material Request",
        "view": "Form",
        "script": SCRIPT_JS,
        "enabled": 1,
    }
    res = requests.post(f"{SITE_URL}/api/resource/Client Script", headers=HEADERS, json=payload)
    if res.status_code not in (200, 201):
        raise ERPNextAPIError(f"Client Script 생성 실패: {res.status_code} - {res.text[:500]}")

    print(f"완료: Client Script 등록됨 (Material Request 폼, name={CLIENT_SCRIPT_NAME})")
    print(f"  API_BASE={API_BASE}")


if __name__ == "__main__":
    main()
