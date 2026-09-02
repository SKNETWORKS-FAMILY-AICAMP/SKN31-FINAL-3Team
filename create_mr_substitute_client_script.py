"""
create_mr_substitute_client_script.py — Material Request 화면에 "AI 대체품
확인" 버튼을 붙이는 ERPNext Client Script를 REST API로 등록/갱신함
(2026-09-01). ERPNext 관리화면을 직접 안 만져도 됨 - Client Script도
그냥 DocType이라 우리가 지금까지 해온 것처럼 REST API로 밀어넣으면 됨.

실행 전 .env에 아래 2개를 추가로 넣어야 함:
  PROCUREMENT_API_BASE_URL - webhook/direct 모드에서 요청자의 브라우저가
    실제로 접근 가능한 FastAPI 주소(예: https://api.example.com).
    localhost를 넣으면 각 요청자 PC 자신의 localhost를 가리켜 다른 PC에서
    항상 실패함. polling 모드는 외부 FastAPI 주소 없이 ERPNext 댓글만 쓴다.
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
INGEST_MODE = os.environ.get("MR_INGEST_MODE", "webhook").strip().lower()

if INGEST_MODE != "polling" and (not API_BASE or not SECRET):
    raise SystemExit("PROCUREMENT_API_BASE_URL / CLIENT_SCRIPT_SECRET을 .env에 먼저 설정하세요.")

# 이 마커로 "우리가 등록한 Client Script"를 찾아서 재실행 시 지우고 다시 만듦.
MARKER = "// PROCUREMENT_AI_SUBSTITUTE_BUTTON_V1"

SCRIPT_JS = f"""{MARKER}
frappe.ui.form.on('Material Request', {{
    refresh: function(frm) {{
        // 대체품 결정은 MR Submit 전 Draft 단계에서만 수행한다.
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


# 공용망의 개발 PC는 ERPNext/요청자 브라우저에서 직접 접근할 수 없다.
# polling 모드에서는 후보/결정을 ERPNext Comment에 저장하고 로컬 FastAPI의
# 폴러가 이를 읽어 LangGraph를 재개한다. 브라우저 요청이 전부 ERPNext
# same-origin 안에서 끝나므로 CORS, 포트포워딩, localhost 문제가 없다.
POLLING_SCRIPT_JS = r"""// PROCUREMENT_AI_SUBSTITUTE_BUTTON_V2_POLLING
function biddingflow_plain_text(html) {
    const node = document.createElement('div');
    node.innerHTML = html || '';
    return (node.innerText || node.textContent || '').replace(/\r/g, '').trim();
}

function biddingflow_load_substitute_comment(frm) {
    return frappe.db.get_list('Comment', {
        filters: {
            reference_doctype: 'Material Request',
            reference_name: frm.doc.name,
            comment_type: 'Comment'
        },
        fields: ['name', 'content', 'creation'],
        order_by: 'creation desc',
        limit: 50
    }).then(function(rows) {
        return (rows || []).find(function(row) {
            return biddingflow_plain_text(row.content).includes(
                '[AI Procurement] 대체품 후보가 확인되었습니다.'
            );
        });
    });
}

function biddingflow_parse_candidates(content) {
    return biddingflow_plain_text(content)
        .split('\n')
        .map(function(line) { return line.trim(); })
        .map(function(line) {
            const match = line.match(/^(\d+)\.\s+(.+)\s+\(([^()]+)\)\s+-\s+재고\s+([\d,.]+)\s+\(([^)]+)\)\s+-\s*(.*)$/);
            if (!match) return null;
            return {
                number: parseInt(match[1], 10),
                item_name: match[2],
                item_code: match[3],
                total_qty: match[4],
                fulfillment: match[5],
                reason: match[6]
            };
        })
        .filter(Boolean);
}

function biddingflow_post_substitute_reply(frm, reply, dialog) {
    return frappe.call({
        method: 'frappe.desk.form.utils.add_comment',
        args: {
            reference_doctype: frm.doc.doctype,
            reference_name: frm.doc.name,
            content: '[BiddingFlow 대체품 선택] ' + reply,
            comment_email: frappe.session.user,
            comment_by: frappe.session.user_fullname || frappe.session.user
        }
    }).then(function() {
        dialog.hide();
        frappe.show_alert({
            message: '선택을 접수했습니다. 처리 결과를 확인하고 있습니다.',
            indicator: 'green'
        }, 5);
        biddingflow_wait_for_mr_update(frm);
    }).catch(function(error) {
        frappe.msgprint('대체품 선택 등록 실패: ' + (error.message || error));
    });
}

function biddingflow_wait_for_mr_update(frm) {
    // 댓글 폴러가 선택을 처리하는 데 수 초가 걸릴 수 있다. 즉시 한 번만
    // reload하면 여전히 Draft인 순간을 읽고 끝나므로, ERP 문서가 실제로
    // Cancelled/Pending이 될 때까지만 짧게 확인한 뒤 화면을 갱신한다.
    let attempts = 0;
    const maxAttempts = 15;

    function check() {
        attempts += 1;
        frappe.db.get_value('Material Request', frm.doc.name, ['docstatus', 'status'])
            .then(function(result) {
                const message = (result || {}).message || {};
                if (Number(message.docstatus) !== 0 || attempts >= maxAttempts) {
                    frm.reload_doc();
                    return;
                }
                window.setTimeout(check, 1000);
            })
            .catch(function() {
                // Draft가 Discard되어 조회 자체가 실패하는 버전도 있으므로
                // 최종 reload로 ERPNext가 현재 상태를 다시 표시하게 한다.
                frm.reload_doc();
            });
    }

    window.setTimeout(check, 800);
}

function biddingflow_show_substitute_dialog(frm, comment) {
    const candidates = biddingflow_parse_candidates(comment.content);
    if (!candidates.length) {
        frappe.msgprint('후보 내용을 해석하지 못했습니다. 문서 타임라인의 AI 안내를 확인해주세요.');
        return;
    }

    let dialog;
    const fields = candidates.map(function(candidate) {
        return {
            fieldtype: 'Button',
            fieldname: 'candidate_' + candidate.number,
            label: candidate.number + '. ' + candidate.item_name +
                ' (' + candidate.item_code + ') · 재고 ' + candidate.total_qty +
                ' · ' + candidate.fulfillment,
            click: function() {
                biddingflow_post_substitute_reply(frm, String(candidate.number), dialog);
            }
        };
    });
    fields.push({
        fieldtype: 'Button',
        fieldname: 'buy_original',
        label: '대체품을 사용하지 않고 신규구매 진행',
        click: function() {
            biddingflow_post_substitute_reply(frm, '구매', dialog);
        }
    });

    dialog = new frappe.ui.Dialog({
        title: 'AI 대체품 추천',
        fields: fields
    });
    dialog.show();
}

frappe.ui.form.on('Material Request', {
    refresh: function(frm) {
        // 신규구매 선택 전까지 MR은 의도적으로 Draft 상태를 유지한다.
        if (frm.doc.docstatus !== 0) return;

        biddingflow_load_substitute_comment(frm).then(function(comment) {
            if (!comment) return;
            frm.add_custom_button('AI 대체품 확인', function() {
                biddingflow_show_substitute_dialog(frm, comment);
            });
        }).catch(function(error) {
            console.warn('[BiddingFlow] 대체품 안내 조회 실패', error);
        });
    }
});
"""

if INGEST_MODE == "polling":
    SCRIPT_JS = POLLING_SCRIPT_JS


# Client Script는 autoname이 "Prompt"라 name을 직접 지정해줘야 함(안 주면
# ERPNext가 "Please set the document name" 에러를 던짐 - 실제로 겪음).
# 공백 없이 고정된 이름으로 둬서 재실행할 때 URL 경로에 그대로 안전하게 씀.
CLIENT_SCRIPT_NAME = "Procurement_AI_Substitute_Button"


def main():
    existing = requests.get(f"{SITE_URL}/api/resource/Client Script/{CLIENT_SCRIPT_NAME}", headers=HEADERS)
    if existing.status_code == 200:
        res = requests.put(
            f"{SITE_URL}/api/resource/Client Script/{CLIENT_SCRIPT_NAME}",
            headers=HEADERS,
            json={"dt": "Material Request", "view": "Form", "script": SCRIPT_JS, "enabled": 1},
        )
        if res.status_code != 200:
            raise ERPNextAPIError(f"Client Script 갱신 실패: {res.status_code} - {res.text[:500]}")
        print(f"완료: Client Script 갱신됨 (mode={INGEST_MODE})")
        return

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
