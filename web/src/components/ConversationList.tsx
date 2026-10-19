/** 会话列表：新会话、按时间分组、搜索、重命名、删除；有一轮正在跑时不能切换 */
import { DeleteOutlined, EditOutlined, MessageOutlined, MoreOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons'
import { Alert, App, Button, Dropdown, Empty, Input, Modal } from 'antd'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router'

import { errorText } from '../api/client'
import type { ConversationSummary } from '../api/types'
import { useChat } from '../state/chat'
import { formatTime, timeGroup } from '../utils/format'

const UNTITLED = '未命名会话'

export default function ConversationList({ onNavigate }: { onNavigate?: () => void }) {
  const { conversations, conversationId, busy, listError, rename, remove } = useChat()
  const navigate = useNavigate()
  const { modal, message } = App.useApp()
  const [keyword, setKeyword] = useState('')
  const [renaming, setRenaming] = useState<ConversationSummary | null>(null)
  const [title, setTitle] = useState('')
  const [saving, setSaving] = useState(false)

  const groups = useMemo(() => {
    const word = keyword.trim()
    const result: [string, ConversationSummary[]][] = []
    for (const item of conversations) {
      if (word && !(item.title || UNTITLED).includes(word)) continue
      const group = timeGroup(item.updated_at)
      const last = result[result.length - 1]
      if (last && last[0] === group) last[1].push(item)
      else result.push([group, [item]])
    }
    return result
  }, [conversations, keyword])

  const go = (path: string) => {
    navigate(path)
    onNavigate?.()
  }

  const saveTitle = async () => {
    if (!renaming || !title.trim()) return
    setSaving(true)
    try {
      await rename(renaming.id, title.trim())
      setRenaming(null)
    } catch (error) {
      message.error(`改名失败：${errorText(error)}`)
    } finally {
      setSaving(false)
    }
  }

  const confirmDelete = (item: ConversationSummary) => {
    modal.confirm({
      title: '删除这个会话？',
      content: `「${item.title || UNTITLED}」里的提问和结果都会删掉，不能恢复。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await remove(item.id)
          message.success('已删除')
        } catch (error) {
          message.error(`删除失败：${errorText(error)}`)
        }
      },
    })
  }

  return (
    <div className="conv-list">
      <Button type="primary" block icon={<PlusOutlined />} disabled={busy} onClick={() => go('/')}>新会话</Button>
      <Input allowClear prefix={<SearchOutlined />} placeholder="搜索会话" value={keyword}
             onChange={(event) => setKeyword(event.target.value)} />
      {listError && <Alert type="error" showIcon title="会话列表读不出来" description={listError} />}
      <div className="conv-groups">
        {groups.map(([group, items]) => (
          <div key={group}>
            <div className="conv-group">{group}</div>
            {items.map((item) => {
              const active = item.id === conversationId
              const locked = busy && !active
              return (
                <div
                  key={item.id}
                  role="button"
                  tabIndex={0}
                  className={`conv-item${active ? ' active' : ''}${locked ? ' locked' : ''}`}
                  title={locked ? '正在查询，跑完再切换' : item.title || UNTITLED}
                  onClick={() => { if (!locked && !active) go(`/c/${item.id}`) }}
                  onKeyDown={(event) => { if (event.key === 'Enter' && !locked) go(`/c/${item.id}`) }}
                >
                  <MessageOutlined className="conv-icon" />
                  <span className="conv-text">
                    <span className="conv-title">{item.title || UNTITLED}</span>
                    <span className="conv-meta">{formatTime(item.updated_at)} · {item.turn_count} 轮</span>
                  </span>
                  <Dropdown
                    trigger={['click']}
                    menu={{
                      items: [
                        { key: 'rename', icon: <EditOutlined />, label: '重命名' },
                        { key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true, disabled: busy && active },
                      ],
                      onClick: ({ key, domEvent }) => {
                        domEvent.stopPropagation()
                        if (key === 'rename') {
                          setRenaming(item)
                          setTitle(item.title)
                        } else {
                          confirmDelete(item)
                        }
                      },
                    }}
                  >
                    <Button type="text" size="small" className="conv-more" icon={<MoreOutlined />}
                            onClick={(event) => event.stopPropagation()} />
                  </Dropdown>
                </div>
              )
            })}
          </div>
        ))}
        {!groups.length && !listError && (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={keyword ? '没有匹配的会话' : '还没有会话'} />
        )}
      </div>
      <Modal
        open={renaming !== null}
        title="重命名会话"
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
        okButtonProps={{ disabled: !title.trim() }}
        onOk={saveTitle}
        onCancel={() => setRenaming(null)}
        destroyOnHidden
      >
        <Input autoFocus maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)}
               onPressEnter={saveTitle} />
      </Modal>
    </div>
  )
}
