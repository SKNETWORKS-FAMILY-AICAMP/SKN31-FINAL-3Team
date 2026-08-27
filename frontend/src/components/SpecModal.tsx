import React from 'react';
import { X, FileText, CheckCircle2, Sliders, ShieldCheck } from 'lucide-react';
import type { Item } from '../types';

interface SpecModalProps {
  item: Item | null;
  onClose: () => void;
}

export const SpecModal: React.FC<SpecModalProps> = ({ item, onClose }) => {
  if (!item) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <FileText size={20} color="#3B82F6" />
            <div>
              <h3>규격 상세 정보 ({item.itemCode})</h3>
              <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                {item.itemName} · {item.department}
              </span>
            </div>
          </div>
          <button className="icon-btn" onClick={onClose}>
            <X size={18} />
          </button>
        </div>

        <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
          {/* 요약 규격 */}
          <div 
            style={{
              backgroundColor: 'rgba(59, 130, 246, 0.1)',
              borderLeft: '4px solid #3B82F6',
              padding: '12px 16px',
              borderRadius: '6px',
              fontSize: '13px',
              fontWeight: 600,
              color: '#93C5FD'
            }}
          >
            기본 규격: {item.specSummary}
          </div>

          {/* 세부 사양 Grid */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
            <div style={{ backgroundColor: 'var(--bg-input)', padding: '12px', borderRadius: '8px' }}>
              <div style={{ fontSize: '11px', color: 'var(--text-dim)', marginBottom: '4px' }}>치수 및 표준</div>
              <div style={{ fontSize: '13px', color: '#fff', fontWeight: 600 }}>{item.fullSpec.dimensions}</div>
            </div>
            <div style={{ backgroundColor: 'var(--bg-input)', padding: '12px', borderRadius: '8px' }}>
              <div style={{ fontSize: '11px', color: 'var(--text-dim)', marginBottom: '4px' }}>재질 / 소재</div>
              <div style={{ fontSize: '13px', color: '#fff', fontWeight: 600 }}>{item.fullSpec.material}</div>
            </div>
            <div style={{ backgroundColor: 'var(--bg-input)', padding: '12px', borderRadius: '8px' }}>
              <div style={{ fontSize: '11px', color: 'var(--text-dim)', marginBottom: '4px' }}>작동 온도 범위</div>
              <div style={{ fontSize: '13px', color: '#fff', fontWeight: 600 }}>{item.fullSpec.operatingTemp}</div>
            </div>
            <div style={{ backgroundColor: 'var(--bg-input)', padding: '12px', borderRadius: '8px' }}>
              <div style={{ fontSize: '11px', color: 'var(--text-dim)', marginBottom: '4px' }}>정격 압력 / 등급</div>
              <div style={{ fontSize: '13px', color: '#fff', fontWeight: 600 }}>{item.fullSpec.pressureRating}</div>
            </div>
          </div>

          {/* 제조사 및 특기사항 */}
          <div style={{ backgroundColor: 'var(--bg-input)', padding: '14px', borderRadius: '8px' }}>
            <div style={{ fontSize: '11px', color: 'var(--text-dim)', marginBottom: '4px' }}>권장 권위 제조사</div>
            <div style={{ fontSize: '13px', color: '#fff', fontWeight: 600, marginBottom: '10px' }}>{item.fullSpec.manufacturer}</div>
            <div style={{ fontSize: '11px', color: 'var(--text-dim)', marginBottom: '4px' }}>특기 사항 및 엔지니어링 메모</div>
            <p style={{ fontSize: '12px', color: 'var(--text-muted)', lineHeight: '1.5' }}>{item.fullSpec.notes}</p>
          </div>

          {/* Item Attributes Checklist */}
          <div>
            <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-muted)', marginBottom: '8px', display: 'flex', alignItems: 'center', gap: '6px' }}>
              <Sliders size={14} color="#F59E0B" />
              <span>Item Attributes 속성 검증</span>
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
              {item.attributes.heatResistant && (
                <span className="badge badge-purple" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <CheckCircle2 size={12} /> 내열성 (Heat Resistant)
                </span>
              )}
              {item.attributes.highPressure && (
                <span className="badge badge-red" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <CheckCircle2 size={12} /> 고압용 (High Pressure)
                </span>
              )}
              {item.attributes.isoCertified && (
                <span className="badge badge-green" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <ShieldCheck size={12} /> ISO 인증 (ISO Certified)
                </span>
              )}
              {item.attributes.waterproof && (
                <span className="badge badge-blue" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <CheckCircle2 size={12} /> 방수 (Waterproof)
                </span>
              )}
              {item.attributes.customizable && (
                <span className="badge badge-gray" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <CheckCircle2 size={12} /> 맞춤제작 (Customizable)
                </span>
              )}
            </div>
          </div>
        </div>

        <div className="modal-footer">
          <button className="btn-primary" onClick={onClose}>
            확인 및 닫기
          </button>
        </div>
      </div>
    </div>
  );
};
