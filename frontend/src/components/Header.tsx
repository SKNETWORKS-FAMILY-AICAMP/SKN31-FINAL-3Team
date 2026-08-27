import React from 'react';
import { Search, Bell, Plus } from 'lucide-react';
import type { NavigationTab } from '../types';

interface HeaderProps {
  currentTab: NavigationTab;
  searchQuery: string;
  setSearchQuery: (query: string) => void;
  onOpenNewMRModal: () => void;
}

const tabTitles: Record<NavigationTab, string> = {
  dashboard: '대시보드',
  'item-register': '아이템 등록',
  'mr-list': 'MR 목록',
  'vendor-select': '협력사 선정 (견적 비교 & AI 추천)',
  'po-manage': 'PO 관리',
};

export const Header: React.FC<HeaderProps> = ({
  currentTab,
  searchQuery,
  setSearchQuery,
  onOpenNewMRModal,
}) => {
  return (
    <header className="top-header">
      <div className="header-title-container">
        <h2>{tabTitles[currentTab]}</h2>
      </div>

      {/* Global Search Input */}
      <div className="search-box">
        <Search size={16} color="#9CA3AF" />
        <input
          type="text"
          placeholder="MR / 품목 / 공급사 / 아이템코드 검색..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
        />
      </div>

      <div className="header-actions">
        <button className="icon-btn" title="알림 (4건)">
          <Bell size={18} />
          <span className="notification-dot" />
        </button>

        <button className="btn-primary" onClick={onOpenNewMRModal}>
          <Plus size={16} />
          <span>신규 MR</span>
        </button>
      </div>
    </header>
  );
};
