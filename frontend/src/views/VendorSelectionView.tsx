import React, { useState } from 'react';
import type { VendorSelectionGroup } from '../types';
import { 
  Bot, 
  Sparkles, 
  FileText, 
  Send, 
  X, 
  Paperclip,
  Award
} from 'lucide-react';

interface VendorSelectionViewProps {
  vendorGroups: VendorSelectionGroup[];
  onSelectSupplier: (groupId: string, supplierId: string) => void;
  onOpenSpecModalByItemCode: (itemCode: string) => void;
}

export const VendorSelectionView: React.FC<VendorSelectionViewProps> = ({
  vendorGroups,
  onSelectSupplier,
}) => {
  // Active selected MR Group
  const [selectedGroup, setSelectedGroup] = useState<VendorSelectionGroup | null>(vendorGroups[0] || null);
  
  // Modals state
  const [showDetailModal, setShowDetailModal] = useState<boolean>(false);
  const [showRankModal, setShowRankModal] = useState<boolean>(false);

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
          5-1) <strong>MR 번호 및 아이템명 묶음 카드</strong>를 클릭하여 상세사항(회신내역)과 <strong>AI 견적 순위 및 자동 PR 전송</strong>을 실행할 수 있습니다.
        </span>
      </div>

      {/* 5-1) MR 번호와 아이템명 묶음 카드 리스트 */}
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
              {/* Header: MR 번호 & 아이템명 묶음 */}
              <div className="vendor-group-header">
                <div className="vendor-group-title">
                  <h3>
                    <span style={{ color: '#60A5FA', fontFamily: 'monospace' }}>{group.mrNo}</span>
                    <span style={{ color: 'var(--text-dim)' }}>|</span>
                    <span>{group.itemName}</span>
                    <span style={{ fontSize: '13px', color: 'var(--text-muted)', fontWeight: 400 }}>
                      ({group.quantity} {group.unit})
                    </span>
                  </h3>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
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

                {/* 5-1) 버튼들: 상세사항 확인 & 견적 순위 */}
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

      {/* 5-1-1) 상세사항 확인 Modal */}
      {showDetailModal && selectedGroup && (
        <div className="modal-overlay" onClick={() => setShowDetailModal(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()} style={{ width: '720px' }}>
            <div className="modal-header">
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <FileText size={20} color="#3B82F6" />
                <h3>MR 내용 및 협력사 회신 상세 내역 ({selectedGroup.mrNo})</h3>
              </div>
              <button className="icon-btn" onClick={() => setShowDetailModal(false)}>
                <X size={18} />
              </button>
            </div>

            <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
              {/* MR 기본 정보 카드 */}
              <div style={{ backgroundColor: 'var(--bg-input)', padding: '14px', borderRadius: '8px' }}>
                <div style={{ fontWeight: 700, color: '#fff', fontSize: '15px', marginBottom: '4px' }}>
                  {selectedGroup.itemName} ({selectedGroup.itemCode})
                </div>
                <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                  요청부서: {selectedGroup.department} | 수량: {selectedGroup.quantity} {selectedGroup.unit} | 희망 납기일: {selectedGroup.targetDueDate}
                </div>
              </div>

              {/* 견적 요청한 협력사들 별 회신 여부, 회신 내용, 회신 첨부자료 목록 */}
              <div>
                <h4 style={{ fontSize: '14px', fontWeight: 600, color: '#fff', marginBottom: '10px' }}>
                  협력사 회신 및 제출 자료 목록 ({selectedGroup.quotations.length}개사)
                </h4>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                  {selectedGroup.quotations.map((q) => (
                    <div 
                      key={q.supplierId}
                      style={{
                        backgroundColor: '#111827',
                        border: '1px solid var(--border-color)',
                        borderRadius: '8px',
                        padding: '14px'
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                        <span style={{ fontWeight: 700, color: '#fff', fontSize: '14px' }}>
                          {q.supplierName}
                        </span>
                        {q.isResponded ? (
                          <span className="badge badge-green">회신 완료</span>
                        ) : (
                          <span className="badge badge-red">미회신 (재요청)</span>
                        )}
                      </div>

                      {q.isResponded ? (
                        <>
                          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', fontSize: '12px', color: 'var(--text-muted)', marginBottom: '8px' }}>
                            <div>견적 단가: <strong style={{ color: '#fff' }}>₩{q.quoteUnitPrice.toLocaleString()}</strong></div>
                            <div>총 견적금액: <strong style={{ color: '#60A5FA' }}>₩{q.quoteTotalPrice.toLocaleString()}</strong></div>
                            <div>제시 납기: <strong style={{ color: '#fff' }}>{q.leadTimeDays}일 소요</strong></div>
                          </div>
                          <div style={{ fontSize: '12px', color: '#D1D5DB', backgroundColor: 'rgba(0,0,0,0.2)', padding: '8px', borderRadius: '4px', marginBottom: '8px' }}>
                            회신 설명: {q.resContent}
                          </div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <span style={{ fontSize: '11px', color: 'var(--text-dim)' }}>회신 첨부자료:</span>
                            {q.resAttachments.map((file, idx) => (
                              <span 
                                key={idx} 
                                style={{
                                  fontSize: '11px',
                                  color: '#60A5FA',
                                  backgroundColor: 'rgba(59, 130, 246, 0.15)',
                                  padding: '2px 8px',
                                  borderRadius: '4px',
                                  display: 'inline-flex',
                                  alignItems: 'center',
                                  gap: '4px',
                                  cursor: 'pointer'
                                }}
                              >
                                <Paperclip size={12} /> {file}
                              </span>
                            ))}
                          </div>
                        </>
                      ) : (
                        <div style={{ fontSize: '12px', color: 'var(--text-dim)' }}>
                          아직 견적이 회신되지 않았습니다. AI가 독촉 이메일을 발송하였습니다.
                        </div>
                      )}
                    </div>
                  ))}
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
