import React, { useState } from 'react';
import type { POItem } from '../types';
import { 
  ShoppingCart, 
  CheckCircle2, 
  XCircle, 
  Clock, 
  FileText, 
  X,
  AlertTriangle,
  FileCheck
} from 'lucide-react';

interface POManagementViewProps {
  poItems: POItem[];
  onCreatePO: (poId: string) => void;
}

export const POManagementView: React.FC<POManagementViewProps> = ({
  poItems,
  onCreatePO,
}) => {
  const [selectedMRDetail, setSelectedMRDetail] = useState<POItem | null>(null);
  const [selectedRejectReason, setSelectedRejectReason] = useState<POItem | null>(null);
  const [approvalModalItem, setApprovalModalItem] = useState<POItem | null>(null);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <div 
        style={{
          backgroundColor: 'rgba(16, 185, 129, 0.08)',
          border: '1px solid rgba(16, 185, 129, 0.2)',
          borderRadius: 'var(--radius-md)',
          padding: '12px 18px',
          fontSize: '13px',
          color: '#6EE7B7',
          display: 'flex',
          alignItems: 'center',
          gap: '10px'
        }}
      >
        <FileCheck size={18} color="#10B981" />
        <span>
          6-1) <strong>PR 협력사 승인여부</strong>를 확인하고, 6-2) <strong>승인 완료 항목에 한해 PO 생성(결재권자 결재)</strong>을 진행할 수 있습니다.
        </span>
      </div>

      {/* PO Management Table */}
      <div className="table-container">
        <table className="custom-table">
          <thead>
            <tr>
              <th>PR 번호</th>
              <th>연동 MR 번호</th>
              <th>요청 부서</th>
              <th>품목명 및 아이템코드</th>
              <th>MR 상세 확인 (6-1)</th>
              <th>선정 협력사</th>
              <th>총 금액</th>
              <th>PR 협력사 승인 여부 (6-1)</th>
              <th>PO 생성 / 거절 사유 확인 (6-2)</th>
            </tr>
          </thead>
          <tbody>
            {poItems.map((item) => (
              <tr key={item.id}>
                {/* PR 번호 */}
                <td>
                  <span style={{ fontFamily: 'monospace', fontWeight: 700, color: '#60A5FA' }}>
                    {item.prNo}
                  </span>
                </td>
                {/* MR 번호 */}
                <td>
                  <span style={{ fontFamily: 'monospace', color: 'var(--text-muted)' }}>
                    {item.mrNo}
                  </span>
                </td>
                {/* 요청 부서 */}
                <td>{item.department}</td>
                {/* 품목명 */}
                <td>
                  <div style={{ fontWeight: 600, color: '#fff' }}>{item.itemName}</div>
                  <div style={{ fontSize: '11px', color: 'var(--text-dim)', fontFamily: 'monospace' }}>
                    {item.itemCode}
                  </div>
                </td>
                {/* 6-1) MR 내용 전체 확인 가능 버튼 */}
                <td>
                  <button 
                    className="spec-clickable-btn"
                    onClick={() => setSelectedMRDetail(item)}
                  >
                    <FileText size={13} />
                    <span>MR 전체 확인</span>
                  </button>
                </td>
                {/* 선정 협력사 */}
                <td style={{ fontWeight: 600, color: '#fff' }}>{item.selectedSupplier}</td>
                {/* 총 금액 */}
                <td style={{ textAlign: 'right', fontWeight: 700, fontFamily: 'monospace', color: '#fff' }}>
                  ₩{item.totalAmount.toLocaleString()}
                </td>
                {/* 6-1) PR에 대한 협력사 승인 여부 */}
                <td>
                  {item.supplierApprovalStatus === 'approved' && (
                    <span className="badge badge-green" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                      <CheckCircle2 size={12} /> 협력사 승인 완료
                    </span>
                  )}
                  {item.supplierApprovalStatus === 'rejected' && (
                    <span className="badge badge-red" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                      <XCircle size={12} /> 협력사 거절
                    </span>
                  )}
                  {item.supplierApprovalStatus === 'pending' && (
                    <span className="badge badge-gray" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                      <Clock size={12} /> 승인 확인 대기 중
                    </span>
                  )}
                </td>
                {/* 6-2) 협력사 PR 승인된 것만 PO 생성 버튼 (결재권자 결재) / 거절 시 사유 기재란 확인 */}
                <td>
                  {item.supplierApprovalStatus === 'approved' ? (
                    item.poCreated ? (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                        <span className="badge badge-blue">
                          ✓ {item.poNo} 생성 완료
                        </span>
                        <span style={{ fontSize: '10px', color: 'var(--text-dim)' }}>
                          {item.createdDate}
                        </span>
                      </div>
                    ) : (
                      <button
                        className="btn-primary"
                        style={{ padding: '6px 12px', fontSize: '12px' }}
                        onClick={() => setApprovalModalItem(item)}
                      >
                        <ShoppingCart size={14} />
                        <span>PO 생성 (결재 진행)</span>
                      </button>
                    )
                  ) : item.supplierApprovalStatus === 'rejected' ? (
                    <button
                      className="btn-reject"
                      style={{ padding: '6px 12px', fontSize: '12px' }}
                      onClick={() => setSelectedRejectReason(item)}
                    >
                      <AlertTriangle size={14} />
                      <span>거절 사유 확인</span>
                    </button>
                  ) : (
                    <button
                      className="btn-outline"
                      disabled
                      style={{ padding: '6px 12px', fontSize: '12px', opacity: 0.5, cursor: 'not-allowed' }}
                    >
                      승인 대기중
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* 6-1) MR 내용 전체 확인 Modal */}
      {selectedMRDetail && (
        <div className="modal-overlay" onClick={() => setSelectedMRDetail(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <FileText size={20} color="#3B82F6" />
                <h3>MR 및 PR 상세 내역 ({selectedMRDetail.prNo})</h3>
              </div>
              <button className="icon-btn" onClick={() => setSelectedMRDetail(null)}>
                <X size={18} />
              </button>
            </div>
            <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              <div style={{ backgroundColor: 'var(--bg-input)', padding: '14px', borderRadius: '8px' }}>
                <div style={{ fontSize: '16px', fontWeight: 700, color: '#fff', marginBottom: '6px' }}>
                  {selectedMRDetail.itemName} ({selectedMRDetail.itemCode})
                </div>
                <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                  연동 MR: {selectedMRDetail.mrNo} | 요청부서: {selectedMRDetail.department} | 희망 납기일: {selectedMRDetail.dueDate}
                </div>
              </div>
              <div style={{ backgroundColor: 'var(--bg-input)', padding: '14px', borderRadius: '8px' }}>
                <div style={{ fontSize: '12px', color: 'var(--text-dim)', marginBottom: '4px' }}>선정 공급사 및 발주 금액</div>
                <div style={{ fontSize: '15px', fontWeight: 700, color: '#10B981' }}>
                  {selectedMRDetail.selectedSupplier} · Total ₩{selectedMRDetail.totalAmount.toLocaleString()}
                </div>
              </div>
            </div>
            <div className="modal-footer">
              <button className="btn-primary" onClick={() => setSelectedMRDetail(null)}>
                확인 완료
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 6-2) PR 거절 사유 확인 Modal */}
      {selectedRejectReason && (
        <div className="modal-overlay" onClick={() => setSelectedRejectReason(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()} style={{ width: '480px' }}>
            <div className="modal-header">
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <AlertTriangle size={20} color="#EF4444" />
                <h3>협력사 PR 거절 사유 확인 ({selectedRejectReason.prNo})</h3>
              </div>
              <button className="icon-btn" onClick={() => setSelectedRejectReason(null)}>
                <X size={18} />
              </button>
            </div>
            <div className="modal-body">
              <div style={{ fontSize: '13px', color: 'var(--text-muted)', marginBottom: '10px' }}>
                공급사: <strong style={{ color: '#fff' }}>{selectedRejectReason.selectedSupplier}</strong>
              </div>
              <div 
                style={{
                  backgroundColor: 'rgba(239, 68, 68, 0.1)',
                  borderLeft: '4px solid #EF4444',
                  padding: '14px',
                  borderRadius: '6px',
                  color: '#FCA5A5',
                  fontSize: '13px',
                  lineHeight: '1.5'
                }}
              >
                {selectedRejectReason.rejectReason || '사유가 작성되지 않았습니다.'}
              </div>
            </div>
            <div className="modal-footer">
              <button className="btn-primary" onClick={() => setSelectedRejectReason(null)}>
                확인 완료
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 6-2) PO 생성 (결재권자 결재) Modal */}
      {approvalModalItem && (
        <div className="modal-overlay" onClick={() => setApprovalModalItem(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()} style={{ width: '500px' }}>
            <div className="modal-header">
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <ShoppingCart size={20} color="#10B981" />
                <h3>PO 생성 및 전자 결재 요청</h3>
              </div>
              <button className="icon-btn" onClick={() => setApprovalModalItem(null)}>
                <X size={18} />
              </button>
            </div>
            <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              <p style={{ fontSize: '13px', color: 'var(--text-muted)' }}>
                협력사({approvalModalItem.selectedSupplier})의 PR 승인이 완료된 항목입니다. 결재권자 승인 후 PO를 최종 생성하시겠습니까?
              </p>
              <div style={{ backgroundColor: 'var(--bg-input)', padding: '12px', borderRadius: '6px', fontSize: '13px' }}>
                <div>발주 품목: <strong>{approvalModalItem.itemName}</strong></div>
                <div>발주 금액: <strong style={{ color: '#10B981' }}>₩{approvalModalItem.totalAmount.toLocaleString()}</strong></div>
              </div>
            </div>
            <div className="modal-footer">
              <button className="btn-outline" onClick={() => setApprovalModalItem(null)}>
                취소
              </button>
              <button 
                className="btn-primary" 
                onClick={() => {
                  onCreatePO(approvalModalItem.id);
                  alert(`PO 결재가 승인되었습니다!\n새로운 발주서 번호 [PO-2025-00${Math.floor(Math.random() * 90 + 10)}]가 성공적으로 발행되었습니다.`);
                  setApprovalModalItem(null);
                }}
              >
                결재 승인 및 PO 생성
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
