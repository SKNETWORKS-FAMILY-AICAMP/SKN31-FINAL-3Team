import React, { useState } from 'react';
import { X, AlertCircle } from 'lucide-react';

interface RejectReasonModalProps {
  title: string;
  itemNo: string;
  onConfirm: (reason: string) => void;
  onClose: () => void;
}

export const RejectReasonModal: React.FC<RejectReasonModalProps> = ({
  title,
  itemNo,
  onConfirm,
  onClose,
}) => {
  const [reason, setReason] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!reason.trim()) {
      alert('반려 사유를 입력해주세요.');
      return;
    }
    onConfirm(reason);
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()} style={{ width: '480px' }}>
        <div className="modal-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <AlertCircle size={20} color="#EF4444" />
            <h3>{title} ({itemNo})</h3>
          </div>
          <button className="icon-btn" onClick={onClose}>
            <X size={18} />
          </button>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="modal-body">
            <div className="form-group">
              <label>반려 상세 사유 작성 (필수)</label>
              <textarea
                className="form-input"
                rows={4}
                placeholder="예: 예산 초과로 인한 사유, 기존 창고 B구역 동일 품목 재고 5개 존재함. 요청 부서 재검토 필요."
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                style={{ resize: 'vertical' }}
                autoFocus
              />
            </div>
          </div>

          <div className="modal-footer">
            <button type="button" className="btn-outline" onClick={onClose}>
              취소
            </button>
            <button type="submit" className="btn-reject">
              반려 확정 처리
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
