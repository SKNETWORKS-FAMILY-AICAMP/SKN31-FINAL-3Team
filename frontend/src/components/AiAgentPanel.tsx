import React from 'react';
import { Bot, Sparkles } from 'lucide-react';
import type { AiLog } from '../types';

interface AiAgentPanelProps {
  logs: AiLog[];
}

export const AiAgentPanel: React.FC<AiAgentPanelProps> = ({ logs }) => {
  return (
    <div className="ai-agent-panel">
      <div className="ai-panel-header">
        <Bot size={20} color="#3B82F6" />
        <h3>AI 에이전트</h3>
        <span className="ai-live-dot" title="실시간 자동 감시 작동중" />
      </div>

      <div className="ai-log-list">
        {logs.map((log) => (
          <div key={log.id} className="ai-log-item">
            <div className="ai-log-top">
              <span className={`status-dot ${log.type}`} />
              <span className="ai-log-title">{log.title}</span>
            </div>
            <p className="ai-log-detail">{log.detail}</p>
            <div className="ai-log-meta">
              <span>{log.time}</span>
              {log.mrNo && (
                <span 
                  style={{
                    backgroundColor: 'rgba(59, 130, 246, 0.15)',
                    color: '#93C5FD',
                    padding: '2px 6px',
                    borderRadius: '4px',
                    fontFamily: 'monospace'
                  }}
                >
                  {log.mrNo}
                </span>
              )}
            </div>
          </div>
        ))}
      </div>

      <div 
        style={{
          marginTop: 'auto',
          paddingTop: '16px',
          borderTop: '1px solid var(--border-color)',
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          fontSize: '11px',
          color: 'var(--text-dim)'
        }}
      >
        <Sparkles size={14} color="#F59E0B" />
        <span>ERPNext 실시간 이벤트 동기화 활성화됨</span>
      </div>
    </div>
  );
};
