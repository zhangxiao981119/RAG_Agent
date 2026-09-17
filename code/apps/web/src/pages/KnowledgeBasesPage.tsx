import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { App, Button, Card, Col, Empty, Input, Modal, Row, Select, Space, Spin, Tag, Typography } from 'antd'
import { PlusOutlined, TeamOutlined, MinusCircleOutlined } from '@ant-design/icons'

import {
  fetchKbs,
  fetchKbMembers,
  KnowledgeBase,
  KbMemberItem,
  setKbMembers,
} from '../mocks/data'

const { Title, Text, Paragraph } = Typography

const SUBJECT_TYPE_COLOR: Record<KbMemberItem['subject_type'], string> = {
  user: 'blue',
  group: 'purple',
  dept: 'green',
  role: 'orange',
}

const SUBJECT_TYPE_LABEL: Record<KbMemberItem['subject_type'], string> = {
  user: '用户',
  group: '用户组',
  dept: '部门',
  role: '角色',
}

export function KnowledgeBasesPage() {
  const { message } = App.useApp()
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetchKbs()
      .then(setKbs)
      .catch((e) => message.error(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [message])

  const [memberModalOpen, setMemberModalOpen] = useState(false)
  const [memberModalKb, setMemberModalKb] = useState<KnowledgeBase | null>(null)

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20 }}>
        <div>
          <Title level={4} style={{ margin: 0 }}>
            知识库
          </Title>
          <Text type="secondary">管理可访问的知识库</Text>
        </div>
        <Button type="primary" icon={<PlusOutlined />}>
          新建知识库
        </Button>
      </div>

      <Spin spinning={loading}>
        {!loading && kbs.length === 0 ? (
          <Card variant="borderless">
            <Empty description="暂无知识库" />
          </Card>
        ) : (
          <Row gutter={[16, 16]}>
            {kbs.map((kb) => (
              <Col xs={24} sm={12} lg={8} key={kb.id}>
                <Card
                  hoverable
                  variant="borderless"
                  styles={{ body: { padding: 20 } }}
                  title={
                    <Link to={`/knowledge-bases/${kb.id}/documents`} style={{ color: 'inherit' }}>
                      <Text strong style={{ fontSize: 15 }}>
                        {kb.name}
                      </Text>
                    </Link>
                  }
                  extra={
                    <Space>
                      {kb.is_public && <Tag color="blue">公开</Tag>}
                      {!kb.is_public && (
                        <Button
                          size="small"
                          type="text"
                          icon={<TeamOutlined />}
                          onClick={(e) => {
                            e.preventDefault()
                            setMemberModalKb(kb)
                            setMemberModalOpen(true)
                          }}
                        >
                          成员
                        </Button>
                      )}
                    </Space>
                  }
                >
                  <Paragraph
                    type="secondary"
                    ellipsis={{ rows: 2 }}
                    style={{ marginTop: 8, marginBottom: 16, minHeight: 44 }}
                  >
                    {kb.description || '暂无描述'}
                  </Paragraph>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {kb.doc_count} 份文档
                  </Text>
                </Card>
              </Col>
            ))}
          </Row>
        )}
      </Spin>

      {memberModalKb && (
        <MemberModal
          open={memberModalOpen}
          kb={memberModalKb}
          onClose={() => {
            setMemberModalOpen(false)
            setMemberModalKb(null)
          }}
          onChanged={() => {
            fetchKbs().then(setKbs).catch(() => {})
          }}
        />
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────
// 成员管理 Modal（M4 任务 8）
//
// 展示当前成员（按 subject_type 分 Tag 颜色），支持：
//   · 填 subject_type + subject_id + label 添加单个成员
//   · 删除已有成员
// 公开库不需要成员管理（所有人自动可见），Modal 对公开库隐藏。
// ─────────────────────────────────────────────────────────────
function MemberModal({
  open,
  kb,
  onClose,
  onChanged,
}: {
  open: boolean
  kb: KnowledgeBase
  onClose: () => void
  onChanged: () => void
}) {
  const { message } = App.useApp()
  const [members, setMembers] = useState<KbMemberItem[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  // 新增成员表单
  const [addType, setAddType] = useState<KbMemberItem['subject_type']>('user')
  const [addId, setAddId] = useState('')
  const [addLabel, setAddLabel] = useState('')

  useEffect(() => {
    if (!open) return
    setLoading(true)
    fetchKbMembers(kb.id)
      .then(setMembers)
      .catch((e) => message.error(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [open, kb.id, message])

  const addMember = () => {
    const id = addId.trim()
    if (!id) {
      message.warning('subject_id 不能为空')
      return
    }
    // 去重
    if (members.some((m) => m.subject_type === addType && m.subject_id === id)) {
      message.warning('该成员已存在')
      return
    }
    setMembers([...members, { subject_type: addType, subject_id: id, label: addLabel.trim() || id }])
    setAddId('')
    setAddLabel('')
  }

  const removeMember = (idx: number) => {
    setMembers(members.filter((_, i) => i !== idx))
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      const r = await setKbMembers(kb.id, members)
      if (r.changed) {
        message.success('成员已更新')
      } else {
        message.info('成员未变化')
      }
      onChanged()
      onClose()
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={`成员管理 · ${kb.name}`}
      open={open}
      onCancel={onClose}
      onOk={handleSave}
      confirmLoading={saving}
      width={640}
      okText="保存（全量替换）"
      cancelText="取消"
    >
      <Spin spinning={loading}>
        {/* 新增表单 */}
        <Card size="small" title="添加成员" style={{ marginBottom: 16 }}>
          <Space wrap>
            <Select
              value={addType}
              onChange={setAddType}
              style={{ width: 100 }}
              options={[
                { value: 'user', label: '用户' },
                { value: 'group', label: '用户组' },
                { value: 'dept', label: '部门' },
                { value: 'role', label: '角色' },
              ]}
            />
            <Input
              placeholder="subject_id (UUID / 路径 / 角色名)"
              value={addId}
              onChange={(e) => setAddId(e.target.value)}
              style={{ width: 240 }}
            />
            <Input
              placeholder="label (展示名，可选)"
              value={addLabel}
              onChange={(e) => setAddLabel(e.target.value)}
              style={{ width: 180 }}
            />
            <Button type="primary" onClick={addMember}>
              添加
            </Button>
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            提示：user/group 填 UUID 字符串；dept 填规整路径如 /总部/财务部/；role 填角色名如 admin
          </Text>
        </Card>

        {/* 当前成员列表 */}
        <Card size="small" title={`当前成员（${members.length}）`}>
          {members.length === 0 ? (
            <Empty description="暂无成员，保存后该库对所有人不可见" />
          ) : (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
              {members.map((m, idx) => (
                <Tag
                  key={`${m.subject_type}:${m.subject_id}`}
                  color={SUBJECT_TYPE_COLOR[m.subject_type]}
                  closable
                  onClose={() => removeMember(idx)}
                  closeIcon={<MinusCircleOutlined />}
                  style={{ padding: '4px 8px', fontSize: 13 }}
                >
                  <span style={{ fontWeight: 500 }}>[{SUBJECT_TYPE_LABEL[m.subject_type]}]</span>{' '}
                  {m.label}
                </Tag>
              ))}
            </div>
          )}
        </Card>
      </Spin>
    </Modal>
  )
}
