import React, { useState } from 'react';
import type { VendorSelectionGroup } from '../types';
import { 
  Bot, 
  Sparkles, 
  FileText, 
  Send, 
  X, 
  Paperclip,
  Award,
  Calendar,
  Clock,
  Mail,
  CheckCircle2,
  XCircle
} from 'lucide-react';

interface VendorSelectionViewProps {
  vendorGroups: VendorSelectionGroup[];
  onSelectSupplier: (groupId: string, supplierId: string) => void;
  onOpenSpecModalByItemCode: (itemCode: string) => void;
  onExtendDeadline: (groupId: string, newDate: string, newTime: string) => void;
}

export const VendorSelectionView: React.FC<VendorSelectionViewProps> = ({
  vendorGroups,
  onSelectSupplier,
  onExtendDeadline,
}) => {
  // Active selected MR Group
  const [selectedGroup, setSelectedGroup] = useState<VendorSelectionGroup | null>(vendorGroups[0] || null);
  
  // Modals state
  const [showDetailModal, setShowDetailModal] = useState<boolean>(false);
  const [showRankModal, setShowRankModal] = useState<boolean>(false);
  const [extendingGroup, setExtendingGroup] = useState<VendorSelectionGroup | null>(null);

  // Extension Modal Form state
  const [extDate, setExtDate] = useState<string>('2025-01-25');
  const [extTime, setExtTime] = useState<string>('18:00');

  const handleOpenExtendModal = (group: VendorSelectionGroup) => {
    setExtendingGroup(group);
    setExtDate(group.deadlineDate || '2025-01-25');
    setExtTime(group.deadlineTime || '18:00');
  };

  const handleConfirmExtension = (e: React.FormEvent) => {
    e.preventDefault();
    if (!extendingGroup) return;
    onExtendDeadline(extendingGroup.id, extDate, extTime);
    alert(`마감시간이 ${extDate} ${extTime}까지 성공적으로 연장되었습니다!\n\n견적을 회신하지 않은 업체에 마감시간 연장 및 독촉 안내 메일이 즉시 자동 발송되었습니다. 📧`);
    setExtendingGroup(null);
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <div 
        style={{
          backgroundColor: 'rgba(59, 130, 246, 0.08)',
          border: '1px solid rgba(59, 130, 246, 0.2)',
          borderRadius: 'var(--radius-md)',
          padding: '12px 18px',
          fontSize: '13px',
          color: '#93C5FD',
          display: 'flex',
          alignItems: 'center',
          gap: '10px'
        }}
      >
        <Sparkles size={18} color="#F59E0B" />
        <span>
          <strong>협력사 선정 및 마감관리</strong>: 마감시간 연장(1, 2번 위치)으로 미회신 업체 메일 발송 및 <strong>[상세사항 확인] 한눈에 보는 비교 표(Table)</strong>를 지원합니다.
        </span>
      </div>

      {/* MR 번호와 아이템명 묶음 카드 리스트 */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {vendorGroups.map((group) => {
          const isCurrentActive = selectedGroup?.id === group.id;
          const respondedCount = group.quotations.filter((q) => q.isResponded).length;
          const totalSuppliers = group.quotations.length;
          const percent = Math.round((respondedCount / totalSuppliers) * 100);
          const bestQuotation = group.quotations.find((q) => q.aiRank === 1);

          return (
            <div 
              key={group.id} 
              className={`vendor-group-card ${isCurrentActive ? 'active' : ''}`}
              style={{
                border: isCurrentActive ? '2px solid var(--primary)' : '1px solid var(--border-color)',
                boxShadow: isCurrentActive ? '0 0 16px rgba(59, 130, 246, 0.25)' : 'none'
              }}
            >
              {/* Header: MR 번호 & 아이템명 묶음 + 위치 1 (마감일수) + 위치 2 (마감시간 연장 버튼) */}
              <div className="vendor-group-header" style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: '12px' }}>
                <div className="vendor-group-title" style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <h3>
                    <span style={{ color: '#60A5FA', fontFamily: 'monospace' }}>{group.mrNo}</span>
                    <span style={{ color: 'var(--text-dim)', margin: '0 6px' }}>|</span>
                    <span>{group.itemName}</span>
                    <span style={{ fontSize: '13px', color: 'var(--text-muted)', fontWeight: 400, marginLeft: '6px' }}>
                      ({group.quantity} {group.unit})
                    </span>
                  </h3>

                  {/* 📍 위치 1: 협력사 선정 마감일수 기재 (이미지 1번 위치) */}
                  <span 
                    className="badge" 
                    style={{
                      backgroundColor: 'rgba(245, 158, 11, 0.18)',
                      color: '#FCD34D',
                      border: '1px solid rgba(245, 158, 11, 0.35)',
                      padding: '4px 10px',
                      borderRadius: '12px',
                      fontSize: '12px',
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: '5px',
                      fontWeight: 700
                    }}
                    title="협력사 견적 제출 마감 일시"
                  >
                    <Clock size={13} color="#F59E0B" />
                    <span>마감 D-{group.deadlineDDay}일 ({group.deadlineDate} {group.deadlineTime} 마감)</span>
                    {group.isExtended && <span style={{ color: '#60A5FA', fontSize: '10px' }}>(연장됨)</span>}
                  </span>
                </div>

                {/* 📍 위치 2: 마감시간 연장 버튼 (이미지 2번 위치) & 회신율 Badge */}
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginLeft: 'auto' }}>
                  {/* 위치 2: 마감시간 연장 버튼 */}
                  <button
                    className="btn-outline"
                    onClick={() => handleOpenExtendModal(group)}
                    style={{
                      padding: '5px 12px',
                      fontSize: '12px',
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: '6px',
                      borderColor: '#F59E0B',
                      color: '#FCD34D',
                      backgroundColor: 'rgba(245, 158, 11, 0.08)'
                    }}
                    title="마감시간을 늘리고 미회신 협력사에 안내 메일을 재발송합니다."
                  >
                    <Calendar size={13} color="#F59E0B" />
                    <span>📅 마감시간 연장</span>
                  </button>

                  <span className="badge badge-purple">
                    협력사 회신율: {respondedCount}/{totalSuppliers}개사 ({percent}%)
                  </span>
                  {group.prSent && (
                    <span className="badge badge-green" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                      <Send size={12} /> {group.prNo} 전송완료
                    </span>
                  )}
                </div>
              </div>

              {/* Body: Card Content */}
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                  <div style={{ fontSize: '13px', color: 'var(--text-muted)' }}>
                    요청 부서: <strong style={{ color: '#fff' }}>{group.department}</strong> · 납기요청일: {group.targetDueDate}
                  </div>
                  {bestQuotation && (
                    <div style={{ fontSize: '13px', color: '#10B981', display: 'flex', alignItems: 'center', gap: '6px', fontWeight: 600 }}>
                      <Award size={16} />
                      <span>AI 1위 추천 공급사: {bestQuotation.supplierName} (₩{bestQuotation.quoteUnitPrice.toLocaleString()} / EA, 납기 {bestQuotation.leadTimeDays}일)</span>
                    </div>
                  )}
                </div>

                {/* 버튼들: 상세사항 확인 & 견적 순위 */}
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  {/* (1) 상세사항 확인 버튼 */}
                  <button
                    className="btn-outline"
                    style={{ padding: '8px 14px', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '6px' }}
                    onClick={() => {
                      setSelectedGroup(group);
                      setShowDetailModal(true);
                    }}
                  >
                    <FileText size={16} color="#60A5FA" />
                    <span>상세사항 확인</span>
                  </button>

                  {/* (2) 견적 순위 (AI 분석) 버튼 */}
                  <button
                    className="btn-primary"
                    style={{ padding: '8px 16px', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '6px' }}
                    onClick={() => {
                      setSelectedGroup(group);
                      setShowRankModal(true);
                    }}
                  >
                    <Bot size={16} />
                    <span>AI 견적 순위 & 업체 선정</span>
                  </button>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* 📍 위치 2 클릭 시: 마감시간 연장 & 미회신 업체 메일 발송 Modal */}
      {extendingGroup && (
        <div className="modal-overlay" onClick={() => setExtendingGroup(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()} style={{ width: '500px' }}>
            <div className="modal-header">
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <Calendar size={22} color="#F59E0B" />
                <div>
                  <h3>견적 제출 마감시간 연장</h3>
                  <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                    {extendingGroup.mrNo} · {extendingGroup.itemName}
                  </span>
                </div>
              </div>
              <button className="icon-btn" onClick={() => setExtendingGroup(null)}>
                <X size={18} />
              </button>
            </div>

            <form onSubmit={handleConfirmExtension}>
              <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                <div 
                  style={{
                    backgroundColor: 'rgba(245, 158, 11, 0.08)',
                    borderLeft: '4px solid #F59E0B',
                    padding: '12px 14px',
                    borderRadius: '6px',
                    fontSize: '12px',
                    color: '#FCD34D',
                    lineHeight: '1.5'
                  }}
                >
                  <Mail size={14} style={{ display: 'inline', marginRight: '6px' }} />
                  마감시간을 연장하면 견적을 회신하지 않은 <strong>미회신 협력사</strong>에게 마감 연장 안내 및 독촉 메일이 즉시 자동 발송됩니다.
                </div>

                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px' }}>
                  <div className="form-group">
                    <label style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                      <Calendar size={13} color="#60A5FA" />
                      연장할 마감 날짜 선택
                    </label>
                    <input
                      type="date"
                      className="form-input"
                      value={extDate}
                      onChange={(e) => setExtDate(e.target.value)}
                      required
                    />
                  </div>

                  <div className="form-group">
                    <label style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                      <Clock size={13} color="#60A5FA" />
                      연장할 마감 시간 선택
                    </label>
                    <input
                      type="time"
                      className="form-input"
                      value={extTime}
                      onChange={(e) => setExtTime(e.target.value)}
                      required
                    />
                  </div>
                </div>

                <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                  현재 설정 변경: <strong style={{ color: '#fff' }}>{extDate} {extTime}</strong> (미회신 업체 자동 리마인드 처리됨)
                </div>
              </div>

              <div className="modal-footer">
                <button type="button" className="btn-outline" onClick={() => setExtendingGroup(null)}>
                  취소
                </button>
                <button type="submit" className="btn-primary" style={{ backgroundColor: '#F59E0B', color: '#000', fontWeight: 700 }}>
                  <Mail size={14} />
                  마감 연장 및 미회신 업체 메일 발송
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* 5-1-1) 상세사항 확인 Modal (요구사항: 한눈에 보는 '표(Table) 형식'으로 개선) */}
      {showDetailModal && selectedGroup && (
        <div className="modal-overlay" onClick={() => setShowDetailModal(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()} style={{ width: '880px' }}>
            <div className="modal-header">
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <FileText size={20} color="#3B82F6" />
                <h3>MR 내용 및 협력사 회신 비교 표 ({selectedGroup.mrNo})</h3>
              </div>
              <button className="icon-btn" onClick={() => setShowDetailModal(false)}>
                <X size={18} />
              </button>
            </div>

            <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
              {/* MR 기본 정보 요약 바 */}
              <div style={{ backgroundColor: 'var(--bg-input)', padding: '14px 18px', borderRadius: '8px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                  <div style={{ fontWeight: 700, color: '#fff', fontSize: '15px' }}>
                    {selectedGroup.itemName} ({selectedGroup.itemCode})
                  </div>
                  <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '2px' }}>
                    요청부서: {selectedGroup.department} · 수량: {selectedGroup.quantity} {selectedGroup.unit} · 희망 납기일: {selectedGroup.targetDueDate}
                  </div>
                </div>
                <div style={{ textAlign: 'right' }}>
                  <span className="badge badge-purple">
                    마감일시: {selectedGroup.deadlineDate} {selectedGroup.deadlineTime}
                  </span>
                </div>
              </div>

              {/* 요구사항 반영: 협력사별 전체 회신 내용을 한눈에 한 줄씩 비교하는 표(Table) */}
              <div>
                <h4 style={{ fontSize: '14px', fontWeight: 600, color: '#fff', marginBottom: '10px' }}>
                  공급사별 회신 현황 종합 비교표 ({selectedGroup.quotations.length}개사)
                </h4>

                <div className="table-container" style={{ border: '1px solid var(--border-color)', borderRadius: '8px' }}>
                  <table className="custom-table" style={{ minWidth: '800px', fontSize: '12px' }}>
                    <thead>
                      <tr>
                        <th>협력사명</th>
                        <th style={{ textAlign: 'center' }}>회신 여부</th>
                        <th style={{ textAlign: 'right' }}>견적 단가</th>
                        <th style={{ textAlign: 'right' }}>총 견적금액</th>
                        <th style={{ textAlign: 'center' }}>제시 납기</th>
                        <th>제출 첨부자료</th>
                        <th>회신 요약 설명</th>
                      </tr>
                    </thead>
                    <tbody>
                      {selectedGroup.quotations.map((q) => (
                        <tr key={q.supplierId} style={{ backgroundColor: q.isSelected ? 'rgba(16, 185, 129, 0.08)' : 'transparent' }}>
                          {/* 협력사명 */}
                          <td style={{ fontWeight: 700, color: '#fff' }}>
                            {q.supplierName}
                            {q.aiRank === 1 && (
                              <span style={{ fontSize: '10px', color: '#10B981', marginLeft: '6px', fontWeight: 800 }}>[AI 1위]</span>
                            )}
                          </td>

                          {/* 회신 여부 */}
                          <td style={{ textAlign: 'center' }}>
                            {q.isResponded ? (
                              <span className="badge badge-green" style={{ display: 'inline-flex', alignItems: 'center', gap: '3px' }}>
                                <CheckCircle2 size={11} /> 회신 완료
                              </span>
                            ) : (
                              <span className="badge badge-red" style={{ display: 'inline-flex', alignItems: 'center', gap: '3px' }}>
                                <XCircle size={11} /> 미회신 (독촉중)
                              </span>
                            )}
                          </td>

                          {/* 견적 단가 */}
                          <td style={{ textAlign: 'right', fontFamily: 'monospace', fontWeight: 600 }}>
                            {q.isResponded ? `₩${q.quoteUnitPrice.toLocaleString()}` : '-'}
                          </td>

                          {/* 총 견적금액 */}
                          <td style={{ textAlign: 'right', fontFamily: 'monospace', fontWeight: 700, color: q.isResponded ? '#60A5FA' : 'var(--text-dim)' }}>
                            {q.isResponded ? `₩${q.quoteTotalPrice.toLocaleString()}` : '-'}
                          </td>

                          {/* 제시 납기 */}
                          <td style={{ textAlign: 'center' }}>
                            {q.isResponded ? (
                              <span style={{ fontWeight: 600, color: '#fff' }}>{q.leadTimeDays}일 소요</span>
                            ) : '-'}
                          </td>

                          {/* 제출 첨부자료 */}
                          <td>
                            {q.resAttachments.length > 0 ? (
                              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                                {q.resAttachments.map((file, idx) => (
                                  <span 
                                    key={idx} 
                                    style={{
                                      fontSize: '11px',
                                      color: '#60A5FA',
                                      backgroundColor: 'rgba(59, 130, 246, 0.15)',
                                      padding: '2px 6px',
                                      borderRadius: '4px',
                                      display: 'inline-flex',
                                      alignItems: 'center',
                                      gap: '3px'
                                    }}
                                  >
                                    <Paperclip size={11} /> {file}
                                  </span>
                                ))}
                              </div>
                            ) : (
                              <span style={{ color: 'var(--text-dim)' }}>없음</span>
                            )}
                          </td>

                          {/* 회신 요약 설명 */}
                          <td style={{ maxWidth: '220px', color: '#D1D5DB' }}>
                            {q.resContent}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>

            <div className="modal-footer">
              <button className="btn-primary" onClick={() => setShowDetailModal(false)}>
                확인 완료
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 5-1-2) 견적 순위 (AI 추천 및 업체 선정 + PR 자동 전송) Modal */}
      {showRankModal && selectedGroup && (
        <div className="modal-overlay" onClick={() => setShowRankModal(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()} style={{ width: '740px' }}>
            <div className="modal-header">
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <Bot size={22} color="#3B82F6" />
                <div>
                  <h3>AI 견적 분석 순위 & 최적 업체 선정 ({selectedGroup.mrNo})</h3>
                  <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                    적합성, 단가, 납기, 품질 점수를 종합 평가하여 랭킹을 산출합니다.
                  </span>
                </div>
              </div>
              <button className="icon-btn" onClick={() => setShowRankModal(false)}>
                <X size={18} />
              </button>
            </div>

            <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              <div className="vendor-rank-list">
                {selectedGroup.quotations
                  .sort((a, b) => a.aiRank - b.aiRank)
                  .map((q) => (
                    <div 
                      key={q.supplierId} 
                      className={`vendor-rank-item ${q.aiRank === 1 ? 'top-rank' : ''}`}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
                        <div className={`rank-badge rank-${q.aiRank}`}>
                          {q.aiRank}
                        </div>
                        <div>
                          <div style={{ fontSize: '15px', fontWeight: 700, color: '#fff', display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <span>{q.supplierName}</span>
                            {q.aiRank === 1 && (
                              <span className="ai-recommend-badge">
                                <Sparkles size={11} /> AI 1위 최적 추천
                              </span>
                            )}
                          </div>
                          <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '2px' }}>
                            단가: ₩{q.quoteUnitPrice.toLocaleString()} · 총액: ₩{q.quoteTotalPrice.toLocaleString()} · 납기: {q.leadTimeDays}일
                          </div>
                          <div style={{ fontSize: '12px', color: '#93C5FD', marginTop: '6px', lineHeight: 1.4 }}>
                            {q.aiReason}
                          </div>
                        </div>
                      </div>

                      <div>
                        {selectedGroup.selectedSupplierId === q.supplierId ? (
                          <span className="badge badge-green" style={{ padding: '8px 12px', fontSize: '12px' }}>
                            ✓ 업체 선정완료 (PR 전송됨)
                          </span>
                        ) : (
                          <button
                            className="btn-primary"
                            style={{ padding: '8px 14px', fontSize: '12px' }}
                            onClick={() => {
                              onSelectSupplier(selectedGroup.id, q.supplierId);
                              alert(`[${q.supplierName}]이(가) 최종 업체로 선정되었습니다!\nPR-2025-${selectedGroup.mrNo.split('-')[2]}가 ERPNext 시스템으로 자동 전송되었습니다.`);
                              setShowRankModal(false);
                            }}
                          >
                            업체 선정 및 PR 자동 전송
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
              </div>
            </div>

            <div className="modal-footer">
              <button className="btn-outline" onClick={() => setShowRankModal(false)}>
                닫기
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
