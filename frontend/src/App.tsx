import { useEffect, useState } from 'react';
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
  POItem,
  SupplierPRResponse,
  WorkflowTask
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
  const [pendingTasks, setPendingTasks] = useState<WorkflowTask[]>([]);

  // Modals state
  const [activeSpecItem, setActiveSpecItem] = useState<Item | null>(null);
  const [rejectingItem, setRejectingItem] = useState<{ id: string; mrNo: string } | null>(null);
  const [activeAttachmentFiles, setActiveAttachmentFiles] = useState<string[] | null>(null);
  const [toastMessage, setToastMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!['vendor-select', 'po-manage'].includes(currentTab)) return;
    let cancelled = false;

    const loadSupplierPRs = async () => {
      const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';
      const accessToken = localStorage.getItem('access_token');
      const headers: Record<string, string> = accessToken
        ? { Authorization: `Bearer ${accessToken}` }
        : {};
      const [prResponse, taskResponse] = await Promise.all([
        fetch(`${apiBaseUrl}/api/procurement/pr`, { credentials: 'include', headers }),
        fetch(`${apiBaseUrl}/api/procurement/tasks?status=PENDING`, { credentials: 'include', headers }),
      ]);
      if (!prResponse.ok || !taskResponse.ok) throw new Error('PR 작업을 불러오지 못했습니다.');
      const data: { items: SupplierPRResponse[]; count: number } = await prResponse.json();
      const taskData: { items: WorkflowTask[]; count: number } = await taskResponse.json();
      if (cancelled) return;
      setPendingTasks(taskData.items);

      const prItems: POItem[] = data.items.map((pr) => {
        const request = requests.find((row) => row.mrNo === pr.mr_name);
        const vendorGroup = vendorGroups.find((row) => row.mrNo === pr.mr_name);
        const quotation = vendorGroup?.quotations.find(
          (row) => row.supplierId === pr.supplier_id || row.supplierName === pr.supplier_id,
        );
        return {
          id: pr.pr_id,
          caseId: pr.case_id,
          prNo: pr.pr_id,
          mrNo: pr.mr_name,
          itemName: request?.itemName ?? vendorGroup?.itemName ?? '-',
          itemCode: request?.itemCode ?? vendorGroup?.itemCode ?? '-',
          department: request?.department ?? vendorGroup?.department ?? '-',
          selectedSupplier: quotation?.supplierName ?? pr.supplier_id,
          supplierEmail: pr.supplier_email,
          totalAmount: quotation?.quoteTotalPrice ?? request?.totalPrice ?? 0,
          dueDate: request?.dueDate ?? vendorGroup?.targetDueDate ?? '-',
          prStatus: pr.status,
          sentAt: pr.sent_at,
          responseDeadline: pr.expires_at,
          respondedAt: pr.responded_at,
          supplierApprovalStatus: pr.status === 'REJECTED'
            ? 'rejected'
            : ['ACCEPTED', 'PO_CREATED', 'PO_FAILED'].includes(pr.status)
              ? 'approved'
              : 'pending',
          rejectReason: pr.rejection_reason,
          poCreated: pr.status === 'PO_CREATED',
          poNo: pr.po_name,
          poError: pr.po_error,
          processingError: pr.processing_error,
          createdDate: pr.updated_at,
        };
      });
      const existingCases = new Set(data.items.map((pr) => pr.case_id));
      const requestItems: POItem[] = taskData.items
        .filter((task) => task.task_type === 'pr_request' && !existingCases.has(task.case_id))
        .map((task) => {
          const mrName = String(task.payload.mr_name ?? '');
          const request = requests.find((row) => row.mrNo === mrName);
          const vendorGroup = vendorGroups.find((row) => row.mrNo === mrName);
          const supplier = String(task.payload.selected_supplier ?? '');
          const quotation = vendorGroup?.quotations.find(
            (row) => row.supplierId === supplier || row.supplierName === supplier,
          );
          return {
            id: task.task_id,
            caseId: task.case_id,
            prNo: 'PR 요청 전',
            mrNo: mrName,
            itemName: request?.itemName ?? vendorGroup?.itemName ?? '-',
            itemCode: request?.itemCode ?? vendorGroup?.itemCode ?? '-',
            department: request?.department ?? vendorGroup?.department ?? '-',
            selectedSupplier: quotation?.supplierName ?? supplier,
            totalAmount: quotation?.quoteTotalPrice ?? request?.totalPrice ?? 0,
            dueDate: request?.dueDate ?? vendorGroup?.targetDueDate ?? '-',
            supplierApprovalStatus: 'pending',
            poCreated: false,
            canRequestPR: true,
          };
        });
      setPoItems([...requestItems, ...prItems]);
    };

    void loadSupplierPRs().catch((error) => showToast(String(error)));
    const timer = window.setInterval(
      () => void loadSupplierPRs().catch(() => undefined),
      10_000,
    );
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [currentTab, requests, vendorGroups]);

  const showToast = (msg: string) => {
    setToastMessage(msg);
    setTimeout(() => {
      setToastMessage(null);
    }, 4000);
  };

  const answerWorkflowTask = async (
    task: WorkflowTask,
    answer: Record<string, unknown>,
  ) => {
    const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';
    const accessToken = localStorage.getItem('access_token');
    const response = await fetch(`${apiBaseUrl}/api/procurement/tasks/${task.task_id}/answer`, {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      },
      body: JSON.stringify({ answer, version: task.version }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new Error(body?.detail ?? '워크플로 작업 처리에 실패했습니다.');
    }
    return response.json();
  };

  const handleProceedOrder = async (groupId: string) => {
    const group = vendorGroups.find((row) => row.id === groupId);
    const task = pendingTasks.find(
      (row) => row.task_type === 'order_start' && String(row.payload.mr_name ?? '') === group?.mrNo,
    );
    if (!task) throw new Error('실제 발주 진행 작업을 찾을 수 없습니다. 구매 케이스 상태를 확인해 주세요.');
    await answerWorkflowTask(task, { decision: 'start_order' });
    setCurrentTab('po-manage');
    showToast('PO 관리 화면에서 PR 요청을 진행해 주세요.');
  };

  const handleProceedOrderTask = async (task: WorkflowTask) => {
    await answerWorkflowTask(task, { decision: 'start_order' });
    setCurrentTab('po-manage');
    showToast('PO 관리 화면에서 PR 요청을 진행해 주세요.');
  };

  const handleRequestPR = async (poId: string) => {
    const task = pendingTasks.find((row) => row.task_id === poId && row.task_type === 'pr_request');
    if (!task) throw new Error('PR 요청 작업을 찾을 수 없습니다. 화면을 새로고침해 주세요.');
    await answerWorkflowTask(task, { decision: 'request_pr' });
    showToast('선정 공급사에 PR 요청 메일을 발송했습니다.');
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

  const handleConfirmReject = async (reason: string) => {
    if (!rejectingItem) return;

    const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';
    const accessToken = localStorage.getItem('access_token');
    const response = await fetch(
      `${apiBaseUrl}/purchase/material-requests/${encodeURIComponent(rejectingItem.mrNo)}/rejection-comment`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        },
        body: JSON.stringify({ reason }),
      }
    );

    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new Error(body?.detail ?? '반려 사유 저장에 실패했습니다.');
    }

    // 화면의 검토 결과만 갱신하며 ERPNext MR 문서는 Draft로 유지된다.
    setRequests((prev) =>
      prev.map((r) =>
        r.id === rejectingItem.id ? { ...r, status: '반려', rejectReason: reason } : r
      )
    );
    setRejectingItem(null);
    showToast(`${rejectingItem.mrNo} 건이 반려되었습니다.`);
  };

  const handleApproveItem = (id: string) => {
    setItems((prev) =>
      prev.map((i) => (i.id === id ? { ...i, status: '승인' } : i))
    );
    showToast('아이템 코드 승인이 완료되었습니다.');
  };

  const handleRejectItem = (id: string, reason: string) => {
    setItems((prev) =>
      prev.map((i) => (i.id === id ? { ...i, status: '반려', rejectReason: reason } : i))
    );
    showToast('아이템 코드 등록이 반려되었습니다.');
  };

  const handleExtendDeadline = (groupId: string, newDate: string, newTime: string) => {
    setVendorGroups((prev) =>
      prev.map((g) =>
        g.id === groupId
          ? {
              ...g,
              deadlineDate: newDate,
              deadlineTime: newTime,
              deadlineDDay: Math.max(1, Math.ceil((new Date(newDate).getTime() - new Date().getTime()) / (1000 * 60 * 60 * 24))),
              isExtended: true,
            }
          : g
      )
    );
    showToast(`마감시간이 ${newDate} ${newTime}까지 연장되었습니다. 미회신 업체에 메일이 재발송되었습니다. 📧`);
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
        status: '승인대기',
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
          return {
            ...group,
            selectedSupplierId: supplierId,
            prSent: false,
            prNo: undefined,
            quotations: group.quotations.map((q) => ({
              ...q,
              isSelected: q.supplierId === supplierId,
            })),
          };
        }
        return group;
      })
    );

    showToast('업체 선정이 완료되었습니다. 발주 진행을 눌러 주세요.');
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

      {/* Main Content Area */}
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
                onApproveItem={handleApproveItem}
                onRejectItem={handleRejectItem}
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
                onExtendDeadline={handleExtendDeadline}
                onProceedOrder={handleProceedOrder}
                orderStartTasks={pendingTasks.filter((task) => task.task_type === 'order_start')}
                onProceedOrderTask={handleProceedOrderTask}
              />
            )}

            {/* Screen 6: PO 관리 */}
            {currentTab === 'po-manage' && (
              <POManagementView
                poItems={poItems}
                onCreatePO={handleCreatePO}
                onRequestPR={handleRequestPR}
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
