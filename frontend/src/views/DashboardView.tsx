import React from 'react';
import { 
  Clock, 
  Users, 
  Scale, 
  ShoppingCart, 
  CheckCircle, 
  XCircle, 
  Eye, 
  AlertTriangle,
  Layers,
  CheckCircle2,
  MinusCircle
} from 'lucide-react';
import type { MaterialRequest, NavigationTab } from '../types';

interface DashboardViewProps {
  requests: MaterialRequest[];
  onApprove: (id: string) => void;
  onOpenRejectModal: (id: string, mrNo: string) => void;
  onOpenSpecModal: (itemCode: string) => void;
  setCurrentTab: (tab: NavigationTab) => void;
}

export const DashboardView: React.FC<DashboardViewProps> = ({
  requests,
  onApprove,
  onOpenRejectModal,
  onOpenSpecModal,
  setCurrentTab,
}) => {
  // 2-1) 내 승인 대기 건수
  const pendingRequests = requests.filter((r) => r.status === '승인대기');
  const pendingCount = pendingRequests.length;
  const urgentPendingCount = pendingRequests.filter((r) => r.isUrgent).length;

  // 2-2) 우리팀 건수 총 몇개인지
  const teamTotalCount = requests.length;

  // 견적 비교 대기 & PO 건수 (KPI stats)
  const quotationWaitCount = requests.filter((r) => r.processStage.quotationProgressPercent > 0 && r.processStage.quotationProgressPercent < 100).length;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      {/* 2-1 & 2-2) Top KPI Summary Cards */}
      <div className="kpi-grid">
        {/* Card 1: 내 승인 대기 */}
        <div className="kpi-card">
          <div className="kpi-top">
            <span className="kpi-title">내 승인 대기</span>
            <div className="kpi-icon warning">
              <Clock size={18} />
            </div>
          </div>
          <div className="kpi-value">
            {pendingCount} <span style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-muted)' }}>건</span>
          </div>
          <div className="kpi-sub" style={{ color: urgentPendingCount > 0 ? '#EF4444' : 'var(--text-dim)', fontWeight: urgentPendingCount > 0 ? 600 : 400 }}>
            {urgentPendingCount > 0 ? `🔥 긴급 ${urgentPendingCount}건 포함` : '긴급 건 없음'}
          </div>
        </div>

        {/* Card 2: 우리팀 총 건수 */}
        <div className="kpi-card">
          <div className="kpi-top">
            <span className="kpi-title">우리팀 총 건수</span>
            <div className="kpi-icon info">
              <Users size={18} />
            </div>
          </div>
          <div className="kpi-value">
            {teamTotalCount} <span style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-muted)' }}>건</span>
          </div>
          <div className="kpi-sub">AI 자동 처리 진행 중 2건</div>
        </div>

        {/* Card 3: 협력사 미선정 건 */}
        <div className="kpi-card">
          <div className="kpi-top">
            <span className="kpi-title">협력사 미선정 건</span>
            <div className="kpi-icon warning">
              <Scale size={18} />
            </div>
          </div>
          <div className="kpi-value">
            {quotationWaitCount} <span style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-muted)' }}>건</span>
          </div>
          <div className="kpi-sub" style={{ color: '#F59E0B' }}>오늘 마감 1건 포함</div>
        </div>

        {/* Card 4: 이번 달 발주 PO */}
        <div className="kpi-card">
          <div className="kpi-top">
            <span className="kpi-title">이번 달 발주 PO</span>
            <div className="kpi-icon success">
              <ShoppingCart size={18} />
            </div>
          </div>
          <div className="kpi-value" style={{ color: '#10B981' }}>
            ₩184M
          </div>
          <div className="kpi-sub">12건 발행 완료</div>
        </div>
      </div>

      {/* 세번째 이미지 반영: MR 번호별 체크표시 단계 진행 현황 표 (Table Trackers) */}
      <div className="process-tracker-card">
        <div className="process-header">
          <h3>
            <Layers size={18} color="#3B82F6" />
            <span>MR 번호별 4단계 진행 현황 (체크표시 트래커)</span>
          </h3>
          <span style={{ fontSize: '12px', color: 'var(--text-dim)' }}>
            체크(✓) 표시로 각 MR의 진행 단계를 한눈에 모니터링합니다.
          </span>
        </div>

        {/* MR Progress Table */}
        <div className="table-container" style={{ border: 'none' }}>
          <table className="custom-table" style={{ minWidth: '950px' }}>
            <thead>
              <tr>
                <th>MR 번호</th>
                <th>요청부서 / 품목명</th>
                <th style={{ textAlign: 'center' }}>1단계: 내 승인 여부</th>
                <th style={{ textAlign: 'center' }}>2단계: 견적 회신 진행율</th>
                <th style={{ textAlign: 'center' }}>3단계: PR 협력사 승인</th>
                <th style={{ textAlign: 'center' }}>4단계: PO 결재·발행</th>
                <th style={{ textAlign: 'center' }}>전체 진행 상태</th>
              </tr>
            </thead>
            <tbody>
              {requests.map((req) => {
                // Calculate step progress
                const step1Done = req.status === '승인';
                const step1Rejected = req.status === '반려';
                const step2Done = req.processStage.quotationProgressPercent === 100;
                const step2Percent = req.processStage.quotationProgressPercent;
                const step3Done = req.processStage.prSupplierApproved === '승인';
                const step3Rejected = req.processStage.prSupplierApproved === '거절';
                const step4Done = req.processStage.poCreated;

                let completedSteps = 0;
                if (step1Done) completedSteps++;
                if (step2Done) completedSteps++;
                if (step3Done) completedSteps++;
                if (step4Done) completedSteps++;

                return (
                  <tr key={req.id}>
                    {/* MR 번호 */}
                    <td>
                      <span style={{ fontFamily: 'monospace', fontWeight: 700, color: '#60A5FA' }}>
                        {req.mrNo}
                      </span>
                    </td>

                    {/* 요청부서 / 품목명 */}
                    <td>
                      <div style={{ fontWeight: 600, color: '#fff' }}>{req.itemName}</div>
                      <div style={{ fontSize: '11px', color: 'var(--text-dim)' }}>
                        {req.department} · {req.requester}
                      </div>
                    </td>

                    {/* 1단계: 내 승인 여부 */}
                    <td style={{ textAlign: 'center' }}>
                      {step1Done ? (
                        <span className="badge badge-green" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                          <CheckCircle2 size={13} color="#10B981" /> 승인 완료 ✓
                        </span>
                      ) : step1Rejected ? (
                        <span className="badge badge-red" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                          <XCircle size={13} color="#EF4444" /> 반려됨 ✕
                        </span>
                      ) : (
                        <span className="badge badge-purple" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                          <Clock size={13} color="#F59E0B" /> 승인 대기
                        </span>
                      )}
                    </td>

                    {/* 2단계: 견적 회신 진행율 */}
                    <td style={{ textAlign: 'center' }}>
                      {step2Done ? (
                        <span className="badge badge-green" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                          <CheckCircle2 size={13} color="#10B981" /> 100% 완료 ✓
                        </span>
                      ) : step2Percent > 0 ? (
                        <div style={{ display: 'inline-flex', flexDirection: 'column', alignItems: 'center', gap: '2px' }}>
                          <span className="badge badge-blue">
                            {step2Percent}% 회신 중
                          </span>
                        </div>
                      ) : (
                        <span className="badge badge-gray" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                          <MinusCircle size={13} color="#6B7280" /> 0% 회신 대기
                        </span>
                      )}
                    </td>

                    {/* 3단계: PR 협력사 승인 */}
                    <td style={{ textAlign: 'center' }}>
                      {step3Done ? (
                        <span className="badge badge-green" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                          <CheckCircle2 size={13} color="#10B981" /> 협력사 승인 ✓
                        </span>
                      ) : step3Rejected ? (
                        <span className="badge badge-red" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                          <XCircle size={13} color="#EF4444" /> 협력사 거절 ✕
                        </span>
                      ) : (
                        <span className="badge badge-gray" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                          <Clock size={13} color="#9CA3AF" /> 대기
                        </span>
                      )}
                    </td>

                    {/* 4단계: PO 결재·발행 */}
                    <td style={{ textAlign: 'center' }}>
                      {step4Done ? (
                        <span className="badge badge-green" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                          <CheckCircle2 size={13} color="#10B981" /> PO 발행 완료 ✓
                        </span>
                      ) : (
                        <span className="badge badge-gray" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                          <MinusCircle size={13} color="#6B7280" /> 발행 대기
                        </span>
                      )}
                    </td>

                    {/* 전체 진행 상태 */}
                    <td style={{ textAlign: 'center' }}>
                      {step1Rejected || step3Rejected ? (
                        <span className="badge badge-red">중단 (반려)</span>
                      ) : completedSteps === 4 ? (
                        <span className="badge badge-green">4/4단계 최종 완료</span>
                      ) : (
                        <span className="badge badge-blue">{completedSteps}/4단계 진행 중</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* 2-3) 내가 처리해야 할 항목 (간단하게 표시) */}
      <div className="todo-section">
        <div className="todo-header">
          <h3>
            <AlertTriangle size={18} color="#F59E0B" />
            <span>내가 처리해야 할 항목</span>
            <span className="count-badge">{pendingCount}건</span>
          </h3>
          <button className="btn-outline" style={{ fontSize: '12px', padding: '4px 10px' }} onClick={() => setCurrentTab('mr-list')}>
            전체 목록 보기 →
          </button>
        </div>

        <div className="todo-card-list">
          {pendingRequests.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-muted)' }}>
              <CheckCircle size={36} color="#10B981" style={{ marginBottom: '10px' }} />
              <p>모든 결재 및 처리 항목이 완료되었습니다.</p>
            </div>
          ) : (
            pendingRequests.map((req) => (
              <div key={req.id} className="todo-card">
                <div className="todo-top">
                  <span className={`badge ${req.isUrgent ? 'badge-red' : 'badge-purple'}`}>
                    {req.isUrgent ? '🔥 긴급 승인대기' : '일반 승인대기'}
                  </span>
                  <span className="mr-code">{req.mrNo}</span>
                  <span style={{ fontSize: '11px', color: 'var(--text-dim)', marginLeft: 'auto' }}>
                    납기요청일: {req.dueDate} (D-{req.dDay})
                  </span>
                </div>

                <div className="todo-title">{req.itemName}</div>
                <div className="todo-subtitle">
                  {req.department} · {req.requester} 담당 · 수량: 20 EA · 예상금액: ₩{req.totalPrice.toLocaleString()}
                </div>

                <div className="ai-summary-box">
                  <span style={{ fontWeight: 700, color: '#60A5FA' }}>🤖 AI 에이전트 요약: </span>
                  {req.fullSpecText.split('\n')[1] || req.specSummary}. 협력사 RFQ 3건 발송 준비 완료.
                </div>

                <div className="todo-footer">
                  <div className="todo-meta">
                    <span>견적 진행률: {req.processStage.quotationProgressPercent}%</span>
                  </div>

                  <div className="action-btn-group">
                    <button className="btn-sm btn-approve" onClick={() => onApprove(req.id)}>
                      <CheckCircle size={14} /> 승인
                    </button>
                    <button className="btn-sm btn-reject" onClick={() => onOpenRejectModal(req.id, req.mrNo)}>
                      <XCircle size={14} /> 반려
                    </button>
                    <button className="btn-sm btn-outline" onClick={() => onOpenSpecModal(req.itemCode)}>
                      <Eye size={14} /> 상세 규격
                    </button>
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
};
