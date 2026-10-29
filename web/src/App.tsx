/** 页面框架：页头（品牌、导航、服务状态、模型下拉框）加两页（查数、元数据）。元数据页按需加载，血缘图的代码不进首屏 */
import { DatabaseOutlined, MessageOutlined } from '@ant-design/icons'
import { Menu, Spin } from 'antd'
import { lazy, Suspense } from 'react'
import { Link, Navigate, Route, Routes, useLocation } from 'react-router'

import ModelPicker from './components/ModelPicker'
import StatusBar from './components/StatusBar'
import QueryPage from './pages/QueryPage'
import { ChatProvider, useChat } from './state/chat'
import { HealthProvider } from './state/health'
import { ModelProvider } from './state/model'

const MetadataPage = lazy(() => import('./pages/MetadataPage'))

function Header() {
  const location = useLocation()
  const { conversationId } = useChat()
  const section = location.pathname.startsWith('/metadata') ? 'metadata' : 'query'
  return (
    <header className="app-header">
      <Link to="/" className="brand">
        <img src="/favicon.svg" alt="" className="brand-logo" />
        <span className="brand-name">专题库查数副驾驶</span>
      </Link>
      <Menu
        mode="horizontal"
        className="app-menu"
        selectedKeys={[section]}
        items={[
          { key: 'query', icon: <MessageOutlined />,
            label: <Link to={conversationId ? `/c/${conversationId}` : '/'}>查数</Link> },
          { key: 'metadata', icon: <DatabaseOutlined />, label: <Link to="/metadata">元数据</Link> },
        ]}
      />
      <StatusBar />
      <ModelPicker />
    </header>
  )
}

export default function App() {
  return (
    <HealthProvider>
      <ModelProvider>
        <ChatProvider>
          <div className="app">
            <Header />
            <div className="app-body">
              <Suspense fallback={<div className="page-loading"><Spin /></div>}>
                <Routes>
                  <Route path="/" element={<QueryPage />} />
                  <Route path="/c/:conversationId" element={<QueryPage />} />
                  <Route path="/metadata" element={<MetadataPage />} />
                  <Route path="*" element={<Navigate to="/" replace />} />
                </Routes>
              </Suspense>
            </div>
          </div>
        </ChatProvider>
      </ModelProvider>
    </HealthProvider>
  )
}
