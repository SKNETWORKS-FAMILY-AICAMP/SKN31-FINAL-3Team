import React, { useState } from 'react';
import type { MaterialRequest } from '../types';
import { 
  Paperclip, 
  Eye, 
  Search, 
  Filter, 
  Clock, 
  CheckCircle, 
  XCircle
} from 'lucide-react';

interface MRListViewProps {
  requests: MaterialRequest[];
  searchQuery: string;
  setSearchQuery: (query: string) => void;
  onOpenSpecModalByItemCode: (itemCode: string) => void;
  onApprove: (id: string) => void;
  onOpenRejectModal: (id: string, mrNo: string) => void;
  onOpenAttachmentsModal: (files: string[]) => void;
}

export const MRListView: React.FC<MRListViewProps> = ({
  requests,
  searchQuery,
  setSearchQuery,
  onOpenSpecModalByItemCode,
  onApprove,
  onOpenRejectModal,
  onOpenAttachmentsModal,
}) => {
  // 4-0) 필터링 (상태별)
  const [statusFilter, setStatusFilter] = useState<string>('전체');
  const [deptFilter, setDeptFilter] = useState<string>('전체');

  // 4-1) 검색어 필터링: MR번호, 품목명, 요청부서, 요청자, 아이템코드, 카테고리 등 모든 필드 대상
  const filteredRequests = requests.filter((req) => {
    const query = searchQuery.toLowerCase();
    const matchesSearch =
      req.mrNo.toLowerCase().includes(query) ||
      req.itemName.toLowerCase().includes(query) ||
      req.itemCode.toLowerCase().includes(query) ||
      req.department.toLowerCase().includes(query) ||
      req.requester.toLowerCase().includes(query) ||
      req.category.toLowerCase().includes(query) ||
      req.specSummary.toLowerCase().includes(query);

    const matchesStatus = statusFilter === '전체' || req.status === statusFilter;
    const matchesDept = deptFilter === '전체' || req.department === deptFilter;

    return matchesSearch && matchesStatus && matchesDept;
  });

  // 4-0) 기본 정렬: 납기요청일 기준 (급한 순 / D-Day 작은 순)
  const sortedRequests = [...filteredRequests].sort((a, b) => {
    return new Date(a.dueDate).getTime() - new Date(b.dueDate).getTime();
  });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      {/* 4-1) 상단 검색창 & 4-0) 필터링 바 */}
      <div className="filter-toolbar">
        {/* 4-1) 검색창 */}
        <div className="search-box" style={{ width: '420px' }}>
          <Search size={16} color="#9CA3AF" />
          <input
            type="text"
            placeholder="4-1) MR번호, 품목명, 요청자, 요청부서, 카테고리 등 전체 검색..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>

        {/* 4-0) 필터링 옵션 */}
        <div className="filter-group">
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', color: 'var(--text-muted)' }}>
            <Filter size={15} />
            <span>단계 필터:</span>
          </div>
          <select className="select-box" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="전체">전체 단계</option>
            <option value="승인대기">승인대기</option>
            <option value="승인">승인완료</option>
            <option value="반려">반려됨</option>
          </select>

          <select className="select-box" value={deptFilter} onChange={(e) => setDeptFilter(e.target.value)}>
            <option value="전체">전체 부서</option>
            <option value="생산1팀">생산1팀</option>
            <option value="설비관리팀">설비관리팀</option>
            <option value="전기제어팀">전기제어팀</option>
            <option value="품질보증팀">품질보증팀</option>
          </select>
        </div>
      </div>

      {/* 4-0) 정렬 안내 바 */}
      <div 
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          backgroundColor: 'rgba(245, 158, 11, 0.08)',
          border: '1px solid rgba(245, 158, 11, 0.2)',
          borderRadius: 'var(--radius-md)',
          padding: '10px 16px',
          fontSize: '12px',
          color: '#FCD34D'
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Clock size={16} />
          <span>4-0) 목록 자동 정렬 기준: <strong>납기요청일 (급한 순 / D-Day 순)</strong>으로 자동 정렬되어 표시됩니다.</span>
        </div>
        <span>조회 건수: {sortedRequests.length}건</span>
      </div>

      {/* 4-2) MR 목록 표 (Table) */}
      <div className="table-container">
        <table className="custom-table">
          <thead>
            <tr>
              <th>요청부서</th>
              <th>요청자</th>
              <th>MR번호</th>
              <th>아이템코드</th>
              <th>아이템그룹(카테고리)</th>
              <th>아이템명</th>
              <th>규격(클릭 시 전체보기)</th>
              <th>첨부파일</th>
              <th>단가</th>
              <th>금액</th>
              <th>납기요청일 (급한순)</th>
              <th>단계 (승인/반려)</th>
            </tr>
          </thead>
          <tbody>
            {sortedRequests.map((req) => (
              <tr key={req.id}>
                {/* 요청부서 */}
                <td>{req.department}</td>
                {/* 요청자 */}
                <td style={{ fontWeight: 500 }}>{req.requester}</td>
                {/* MR번호 */}
                <td>
                  <span style={{ fontFamily: 'monospace', fontWeight: 700, color: '#60A5FA' }}>
                    {req.mrNo}
                  </span>
                </td>
                {/* 아이템코드 */}
                <td>
                  <span style={{ fontFamily: 'monospace', color: 'var(--text-muted)' }}>
                    {req.itemCode}
                  </span>
                </td>
                {/* 아이템그룹(카테고리) */}
                <td>
                  <span className="badge badge-gray">{req.category}</span>
                </td>
                {/* 아이템명 */}
                <td style={{ fontWeight: 600, color: '#fff' }}>{req.itemName}</td>
                {/* 규격(클릭 시 전체 내용 확인할 수 있게) */}
                <td>
                  <button className="spec-clickable-btn" onClick={() => onOpenSpecModalByItemCode(req.itemCode)}>
                    <Eye size={13} />
                    <span>{req.specSummary}</span>
                  </button>
                </td>
                {/* 첨부파일 이모티콘: 색깔로 표시되면 첨부파일 있는 거, 색깔 없으면 없는 거 */}
                <td>
                  {req.hasAttachment ? (
                    <div 
                      className="attachment-icon has-file" 
                      onClick={() => onOpenAttachmentsModal(req.attachmentFiles)}
                      title={`첨부파일 ${req.attachmentCount}개 있음 (클릭 시 확인)`}
                      style={{ cursor: 'pointer' }}
                    >
                      <Paperclip size={16} color="#60A5FA" />
                      <span>{req.attachmentCount}</span>
                    </div>
                  ) : (
                    <div className="attachment-icon no-file" title="첨부파일 없음">
                      <Paperclip size={16} color="#6B7280" />
                      <span>0</span>
                    </div>
                  )}
                </td>
                {/* 단가 */}
                <td style={{ textAlign: 'right', fontFamily: 'monospace' }}>
                  ₩{req.unitPrice.toLocaleString()}
                </td>
                {/* 금액 */}
                <td style={{ textAlign: 'right', fontWeight: 700, color: '#fff', fontFamily: 'monospace' }}>
                  ₩{req.totalPrice.toLocaleString()}
                </td>
                {/* 납기요청일 */}
                <td>
                  <div style={{ display: 'flex', flexDirection: 'column' }}>
                    <span style={{ fontWeight: 600, color: req.dDay <= 3 ? '#EF4444' : '#fff' }}>
                      {req.dueDate}
                    </span>
                    <span style={{ fontSize: '11px', color: req.dDay <= 3 ? '#FCA5A5' : 'var(--text-dim)' }}>
                      D-{req.dDay} {req.isUrgent ? '🔥 긴급' : ''}
                    </span>
                  </div>
                </td>
                {/* 단계(승인/반려 - 반려 시 사유기재할 수 있는 칸 생성) */}
                <td>
                  {req.status === '승인' && (
                    <span className="badge badge-green" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                      <CheckCircle size={13} /> 승인 완료
                    </span>
                  )}
                  {req.status === '반려' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                      <span className="badge badge-red" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                        <XCircle size={13} /> 반려됨
                      </span>
                      {req.rejectReason && (
                        <span 
                          style={{ fontSize: '11px', color: '#FCA5A5', maxWidth: '140px', wordBreak: 'break-all' }}
                          title={`반려 사유: ${req.rejectReason}`}
                        >
                          사유: {req.rejectReason}
                        </span>
                      )}
                    </div>
                  )}
                  {req.status === '승인대기' && (
                    <div className="action-btn-group">
                      <button className="btn-sm btn-approve" onClick={() => onApprove(req.id)}>
                        승인
                      </button>
                      <button className="btn-sm btn-reject" onClick={() => onOpenRejectModal(req.id, req.mrNo)}>
                        반려
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};
