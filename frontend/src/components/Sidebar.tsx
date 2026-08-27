import React from 'react';
import { 
  LayoutDashboard, 
  PackagePlus, 
  FileText, 
  Users, 
  ShoppingCart,
  Layers
} from 'lucide-react';
import type { NavigationTab } from '../types';

interface SidebarProps {
  currentTab: NavigationTab;
  setCurrentTab: (tab: NavigationTab) => void;
  pendingCount: number;
}

export const Sidebar: React.FC<SidebarProps> = ({
  currentTab,
  setCurrentTab,
  pendingCount,
}) => {
  return (
    <aside className="sidebar">
      {/* App Branding */}
      <div className="sidebar-header">
        <div className="logo-badge">
          <ShoppingCart size={20} />
        </div>
        <div className="logo-text">
          <h1>구매 Agent</h1>
          <span>ERPNext 연동 v2.4</span>
        </div>
      </div>

      {/* Main Navigation Menu */}
      <div className="sidebar-section-label">메뉴</div>
      <ul className="nav-list">
        {/* 1-1) 대시보드 (MR 전체 단계 안내 포함) */}
        <li
          className={`nav-item ${currentTab === 'dashboard' ? 'active' : ''}`}
          onClick={() => setCurrentTab('dashboard')}
          title="대시보드 - MR 전체 단계 (승인여부, 견적 진행율 %, PR 승인, PO 생성) 확인"
        >
          <div className="nav-item-left">
            <LayoutDashboard size={18} />
            <span>대시보드</span>
          </div>
          {pendingCount > 0 && <span className="nav-badge">{pendingCount}</span>}
        </li>

        {/* 1-2) 아이템 등록 */}
        <li
          className={`nav-item ${currentTab === 'item-register' ? 'active' : ''}`}
          onClick={() => setCurrentTab('item-register')}
        >
          <div className="nav-item-left">
            <PackagePlus size={18} />
            <span>아이템 등록</span>
          </div>
        </li>

        {/* 1-3) MR 목록 */}
        <li
          className={`nav-item ${currentTab === 'mr-list' ? 'active' : ''}`}
          onClick={() => setCurrentTab('mr-list')}
        >
          <div className="nav-item-left">
            <FileText size={18} />
            <span>MR 목록</span>
          </div>
        </li>

        {/* 1-4) 협력사 선정 */}
        <li
          className={`nav-item ${currentTab === 'vendor-select' ? 'active' : ''}`}
          onClick={() => setCurrentTab('vendor-select')}
        >
          <div className="nav-item-left">
            <Users size={18} />
            <span>협력사 선정</span>
          </div>
        </li>

        {/* 1-5) PO 관리 */}
        <li
          className={`nav-item ${currentTab === 'po-manage' ? 'active' : ''}`}
          onClick={() => setCurrentTab('po-manage')}
        >
          <div className="nav-item-left">
            <ShoppingCart size={18} />
            <span>PO 관리</span>
          </div>
        </li>
      </ul>

      {/* Process Stages Mini Indicator inside Sidebar */}
      <div className="sidebar-section-label">MR 단계 트래킹 시스템</div>
      <div 
        style={{
          backgroundColor: 'rgba(255,255,255,0.03)',
          border: '1px solid var(--border-color)',
          borderRadius: 'var(--radius-md)',
          padding: '12px',
          fontSize: '11px',
          color: 'var(--text-muted)',
          display: 'flex',
          flexDirection: 'column',
          gap: '8px'
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: '#60A5FA', fontWeight: 600 }}>
          <Layers size={13} />
          <span>전체 프로세스 4단계</span>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
          <div>1. 내 승인 여부</div>
          <div>2. 견적 회신 진행율 (%)</div>
          <div>3. PR 협력사 승인</div>
          <div>4. PO 결재 및 생성</div>
        </div>
      </div>

      {/* User Info Footer */}
      <div className="sidebar-user">
        <div className="user-avatar">김</div>
        <div className="user-info">
          <h4>김구매 담당자</h4>
          <p>구매관리팀</p>
        </div>
      </div>
    </aside>
  );
};
