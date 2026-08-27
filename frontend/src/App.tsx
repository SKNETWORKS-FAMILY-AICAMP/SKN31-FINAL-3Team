import { useState } from 'react';
import { Sidebar } from './components/Sidebar';
import { Header } from './components/Header';
import { SpecModal } from './components/SpecModal';
import { RejectReasonModal } from './components/RejectReasonModal';

import { DashboardView } from './views/DashboardView';
import { ItemRegistrationView } from './views/ItemRegistrationView';
import { MRListView } from './views/MRListView';
import { VendorSelectionView } from './views/VendorSelectionView';
import { POManagementView } from './views/POManagementView';

import type { 
  NavigationTab, 
  Item, 
  MaterialRequest, 
  VendorSelectionGroup, 
  POItem
} from './types';

import { 
  initialItems, 
  initialMaterialRequests, 
  initialVendorGroups, 
  initialPOItems
} from './mock/data';

import './App.css';
import { Paperclip, X } from 'lucide-react';

export function App() {
  // Navigation & Search
  const [currentTab, setCurrentTab] = useState<NavigationTab>('dashboard');
  const [searchQuery, setSearchQuery] = useState<string>('');

  // Domain State
  const [items, setItems] = useState<Item[]>(initialItems);
  const [requests, setRequests] = useState<MaterialRequest[]>(initialMaterialRequests);
  const [vendorGroups, setVendorGroups] = useState<VendorSelectionGroup[]>(initialVendorGroups);
  const [poItems, setPoItems] = useState<POItem[]>(initialPOItems);

  // Modals state
  const [activeSpecItem, setActiveSpecItem] = useState<Item | null>(null);
  const [rejectingItem, setRejectingItem] = useState<{ id: string; mrNo: string } | null>(null);
  const [activeAttachmentFiles, setActiveAttachmentFiles] = useState<string[] | null>(null);
  const [toastMessage, setToastMessage] = useState<string | null>(null);

  const showToast = (msg: string) => {
    setToastMessage(msg);
    setTimeout(() => {
      setToastMessage(null);
    }, 4000);
  };

  // Actions
  const handleApproveRequest = (id: string) => {
    setRequests((prev) =>
      prev.map((r) =>
        r.id === id ? { ...r, status: '승인', processStage: { ...r.processStage, approval: '완료' } } : r
      )
    );
    showToast('MR 요청 승인이 성공적으로 완료되었습니다.');
  };

  const handleConfirmReject = (reason: string) => {
    if (!rejectingItem) return;
    setRequests((prev) =>
      prev.map((r) =>
        r.id === rejectingItem.id ? { ...r, status: '반려', rejectReason: reason } : r
      )
    );
    setRejectingItem(null);
    showToast(`${rejectingItem.mrNo} 건이 반려되었습니다.`);
  };

  const handleOpenSpecByItemCode = (itemCode: string) => {
    const found = items.find((i) => i.itemCode === itemCode);
    if (found) {
      setActiveSpecItem(found);
    } else {
      setActiveSpecItem({
        id: 'virtual',
        itemCode,
        department: '구매팀',
        itemName: '품목 규격 정보',
        specSummary: 'stroke 300mm / 210bar 정격 / 바이톤 씰',
        fullSpec: {
          dimensions: '표준 규격 치수',
          material: 'SCM440 합금강 (특수 열처리)',
          operatingTemp: '-20°C ~ 180°C',
          pressureRating: '210 bar 고압용',
          manufacturer: 'ISO 9001 승인 브랜드',
          notes: '도면 및 성적서 첨부 완료됨',
        },
        maintainStock: true,
        isFixedAsset: false,
        attributes: { heatResistant: true, highPressure: true, isoCertified: true, waterproof: false, customizable: false },
        registeredDate: '2025-01-01',
        status: '정상',
      });
    }
  };

  const handleAddItem = (newItem: Item) => {
    setItems((prev) => [newItem, ...prev]);
    showToast(`신규 아이템 [${newItem.itemCode}] ${newItem.itemName}이(가) 등록되었습니다.`);
  };

  const handleSelectSupplier = (groupId: string, supplierId: string) => {
    setVendorGroups((prev) =>
      prev.map((group) => {
        if (group.id === groupId) {
          const supplier = group.quotations.find((q) => q.supplierId === supplierId);
          const prNo = `PR-2025-${group.mrNo.split('-')[2] || '0890'}`;

          if (supplier) {
            setPoItems((poPrev) => [
              {
                id: `PO-ITEM-${Date.now()}`,
                prNo,
                mrNo: group.mrNo,
                itemName: group.itemName,
                itemCode: group.itemCode,
                department: group.department,
                selectedSupplier: supplier.supplierName,
                totalAmount: supplier.quoteTotalPrice,
                dueDate: group.targetDueDate,
                supplierApprovalStatus: 'approved',
                poCreated: false,
              },
              ...poPrev,
            ]);
          }

          // Update requests processStage
          setRequests((reqPrev) =>
            reqPrev.map((r) =>
              r.mrNo === group.mrNo
                ? { ...r, processStage: { ...r.processStage, prSupplierApproved: '승인' } }
                : r
            )
          );

          return {
            ...group,
            selectedSupplierId: supplierId,
            prSent: true,
            prNo,
            quotations: group.quotations.map((q) => ({
              ...q,
              isSelected: q.supplierId === supplierId,
            })),
          };
        }
        return group;
      })
    );

    showToast(`업체 선정이 완료되어 PR이 ERPNext로 자동 전송되었습니다.`);
  };

  const handleCreatePO = (poId: string) => {
    setPoItems((prev) =>
      prev.map((item) => {
        if (item.id === poId) {
          // Update request stage
          setRequests((reqPrev) =>
            reqPrev.map((r) =>
              r.mrNo === item.mrNo
                ? { ...r, processStage: { ...r.processStage, poCreated: true } }
                : r
            )
          );

          return {
            ...item,
            poCreated: true,
            poNo: `PO-2025-00${Math.floor(Math.random() * 90 + 10)}`,
            createdDate: new Date().toLocaleString('ko-KR', { hour12: false }),
          };
        }
        return item;
      })
    );
    showToast('PO 생성 및 결재 권자 승인이 최종 승인되었습니다.');
  };

  const pendingCount = requests.filter((r) => r.status === '승인대기').length;

  return (
    <div className="app-container">
      {/* 1. 왼쪽 사이드바 */}
      <Sidebar
        currentTab={currentTab}
        setCurrentTab={setCurrentTab}
        pendingCount={pendingCount}
      />

      {/* Main Content Area - Takes full width */}
      <div className="main-wrapper">
        {/* Header */}
        <Header
          currentTab={currentTab}
          searchQuery={searchQuery}
          setSearchQuery={setSearchQuery}
          onOpenNewMRModal={() => setCurrentTab('mr-list')}
        />

        {/* Content Body (Full Width) */}
        <div className="content-body">
          <main className="view-content">
            {/* Screen 2: 대시보드 */}
            {currentTab === 'dashboard' && (
              <DashboardView
                requests={requests}
                onApprove={handleApproveRequest}
                onOpenRejectModal={(id, mrNo) => setRejectingItem({ id, mrNo })}
                onOpenSpecModal={handleOpenSpecByItemCode}
                setCurrentTab={setCurrentTab}
              />
            )}

            {/* Screen 3: 아이템 등록 */}
            {currentTab === 'item-register' && (
              <ItemRegistrationView
                items={items}
                onOpenSpecModal={setActiveSpecItem}
                onAddItem={handleAddItem}
              />
            )}

            {/* Screen 4: MR 목록 */}
            {currentTab === 'mr-list' && (
              <MRListView
                requests={requests}
                searchQuery={searchQuery}
                setSearchQuery={setSearchQuery}
                onOpenSpecModalByItemCode={handleOpenSpecByItemCode}
                onApprove={handleApproveRequest}
                onOpenRejectModal={(id, mrNo) => setRejectingItem({ id, mrNo })}
                onOpenAttachmentsModal={(files) => setActiveAttachmentFiles(files)}
              />
            )}

            {/* Screen 5: 협력사 선정 */}
            {currentTab === 'vendor-select' && (
              <VendorSelectionView
                vendorGroups={vendorGroups}
                onSelectSupplier={handleSelectSupplier}
                onOpenSpecModalByItemCode={handleOpenSpecByItemCode}
              />
            )}

            {/* Screen 6: PO 관리 */}
            {currentTab === 'po-manage' && (
              <POManagementView
                poItems={poItems}
                onCreatePO={handleCreatePO}
              />
            )}
          </main>
        </div>
      </div>

      {/* 규격 전체 보기 Modal */}
      <SpecModal
        item={activeSpecItem}
        onClose={() => setActiveSpecItem(null)}
      />

      {/* 반려 사유 작성 Modal */}
      {rejectingItem && (
        <RejectReasonModal
          title="MR 요청 반려"
          itemNo={rejectingItem.mrNo}
          onConfirm={handleConfirmReject}
          onClose={() => setRejectingItem(null)}
        />
      )}

      {/* Attachment View Modal */}
      {activeAttachmentFiles && (
        <div className="modal-overlay" onClick={() => setActiveAttachmentFiles(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()} style={{ width: '450px' }}>
            <div className="modal-header">
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <Paperclip size={20} color="#3B82F6" />
                <h3>MR 첨부파일 목록 ({activeAttachmentFiles.length}개)</h3>
              </div>
              <button className="icon-btn" onClick={() => setActiveAttachmentFiles(null)}>
                <X size={18} />
              </button>
            </div>
            <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              {activeAttachmentFiles.map((file, idx) => (
                <div 
                  key={idx}
                  style={{
                    backgroundColor: 'var(--bg-input)',
                    padding: '12px 16px',
                    borderRadius: '8px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    fontSize: '13px',
                    color: '#fff'
                  }}
                >
                  <span>📄 {file}</span>
                  <button className="btn-sm btn-outline">다운로드</button>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Toast Notification Popup */}
      {toastMessage && (
        <div className="toast-container">
          <div className="toast">
            <span>✨ {toastMessage}</span>
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
