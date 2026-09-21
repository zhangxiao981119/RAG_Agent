import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { App, Button, Card, Checkbox, Col, Empty, Input, Modal, Popconfirm, Row, Select, Space, Spin, Tag, Typography } from 'antd'
import { PlusOutlined, TeamOutlined, MinusCircleOutlined, DeleteOutlined } from '@ant-design/icons'

import {
  createKb,
  deleteKb,
  fetchKbs,
  fetchKbMembers,
  fetchUsers,
  fetchGroups,
  fetchRoles,
  fetchDepartments,
  DepartmentNode,
  getStoredUser,
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
  // 前端搜索关键字（按知识库名过滤）
  const [search, setSearch] = useState('')

  useEffect(() => {
    fetchKbs()
      .then(setKbs)
      .catch((e) => message.error(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [message])

  const [memberModalOpen, setMemberModalOpen] = useState(false)
  const [memberModalKb, setMemberModalKb] = useState<KnowledgeBase | null>(null)

  // 新建知识库 Modal
  const [createOpen, setCreateOpen] = useState(false)
  const [createForm, setCreateForm] = useState({ name: '', description: '', isPublic: false })
  const [creating, setCreating] = useState(false)

  const handleCreate = async () => {
    const name = createForm.name.trim()
    if (!name) {
      message.warning('请填写知识库名称')
      return
    }
    setCreating(true)
    try {
      const kb = await createKb(name, createForm.description.trim(), createForm.isPublic)
      message.success(`知识库「${kb.name}」已创建`)
      setCreateOpen(false)
      setCreateForm({ name: '', description: '', isPublic: false })
      fetchKbs().then(setKbs).catch(() => {})
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e))
    } finally {
      setCreating(false)
    }
  }

  // 前端搜索过滤：按知识库名匹配（大小写不敏感），空关键字返回全部
  const filteredKbs = kbs.filter((kb) => {
    const q = search.trim().toLowerCase()
    if (!q) return true
    return kb.name.toLowerCase().includes(q)
  })

  const handleDelete = async (kb: KnowledgeBase) => {
    try {
      await deleteKb(kb.id)
      message.success(`知识库「${kb.name}」已删除`)
      fetchKbs().then(setKbs).catch(() => {})
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20, gap: 12, flexWrap: 'wrap' }}>
        <div>
          <Title level={4} style={{ margin: 0 }}>
            知识库
          </Title>
          <Text type="secondary">管理可访问的知识库</Text>
        </div>
        <Space>
          <Input.Search
            placeholder="按知识库名搜索"
            allowClear
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ width: 240 }}
          />
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              setCreateForm({ name: '', description: '', isPublic: false })
              setCreateOpen(true)
            }}
          >
            新建知识库
          </Button>
        </Space>
      </div>

      <Spin spinning={loading}>
        {!loading && filteredKbs.length === 0 ? (
          <Card variant="borderless">
            <Empty description={search.trim() ? '未匹配到知识库' : '暂无知识库'} />
          </Card>
        ) : (
          <Row gutter={[16, 16]}>
            {filteredKbs.map((kb) => (
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
                      {/* 成员管理按钮仅 admin（clearance >= 40）可见，避免普通用户点开触发 admin API 403 */}
                      {!kb.is_public && (getStoredUser()?.clearance ?? 0) >= 40 && (
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
                      <Popconfirm
                        title="删除知识库"
                        description={`确定删除「${kb.name}」及其全部文档？此操作不可恢复。`}
                        onConfirm={(e) => {
                          e?.preventDefault()
                          handleDelete(kb)
                        }}
                        onCancel={(e) => e?.preventDefault()}
                        okText="删除"
                        cancelText="取消"
                        okButtonProps={{ danger: true }}
                      >
                        <Button
                          size="small"
                          type="text"
                          danger
                          icon={<DeleteOutlined />}
                          onClick={(e) => e.preventDefault()}
                        />
                      </Popconfirm>
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

      {/* 新建知识库 Modal：创建后后端自动把创建者登记为库成员 */}
      <Modal
        title="新建知识库"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={handleCreate}
        confirmLoading={creating}
        okText="创建"
        cancelText="取消"
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <div>
            <Text strong>名称</Text>
            <Input
              placeholder="知识库名称"
              value={createForm.name}
              maxLength={100}
              onChange={(e) => setCreateForm((f) => ({ ...f, name: e.target.value }))}
              style={{ marginTop: 4 }}
            />
          </div>
          <div>
            <Text strong>描述</Text>
            <Input.TextArea
              placeholder="知识库用途描述（可选）"
              value={createForm.description}
              maxLength={500}
              autoSize={{ minRows: 3, maxRows: 6 }}
              onChange={(e) => setCreateForm((f) => ({ ...f, description: e.target.value }))}
              style={{ marginTop: 4 }}
            />
          </div>
          <Checkbox
            checked={createForm.isPublic}
            onChange={(e) => setCreateForm((f) => ({ ...f, isPublic: e.target.checked }))}
          >
            公开库（所有登录用户可见；不勾选则仅成员可见）
          </Checkbox>
        </Space>
      </Modal>
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

  // 新增成员表单：类型决定主体来源，下拉选择（user/group 存 UUID，dept 存路径，role 存角色名）
  const [addType, setAddType] = useState<KbMemberItem['subject_type']>('user')
  const [addOptions, setAddOptions] = useState<{ value: string; label: string }[]>([])
  const [addLoading, setAddLoading] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  // 打开 Modal 时拉取已有成员
  useEffect(() => {
    if (!open) return
    setLoading(true)
    fetchKbMembers(kb.id)
      .then(setMembers)
      .catch((e) => message.error(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [open, kb.id, message])

  useEffect(() => {
    if (!open) return
    // 双重防御：按钮已按 clearance 守卫，Modal 内部再校验一次，避免任何绕过按钮的调用触发 admin API 403
    if ((getStoredUser()?.clearance ?? 0) < 40) return
    setSelectedId(null)
    setAddLoading(true)
    const load = async (): Promise<{ value: string; label: string }[]> => {
      if (addType === 'user') {
        const page = await fetchUsers(1, 200)
        return page.items.map((u) => ({ value: u.id, label: `${u.display_name} (${u.username})` }))
      }
      if (addType === 'group') {
        const groups = await fetchGroups()
        return groups.map((g) => ({ value: g.id, label: g.name }))
      }
      if (addType === 'dept') {
        const flatten = (nodes: DepartmentNode[]): DepartmentNode[] =>
          nodes.flatMap((n) => [n, ...flatten(n.children)])
        const tree = await fetchDepartments()
        return flatten(tree).map((d) => ({ value: d.path, label: d.path }))
      }
      const roles = await fetchRoles()
      return roles.map((r) => ({ value: r.name, label: r.name }))
    }
    load()
      .then(setAddOptions)
      .catch((e) => message.error(e instanceof Error ? e.message : String(e)))
      .finally(() => setAddLoading(false))
  }, [open, addType, message])

  const addMember = () => {
    if (!selectedId) {
      message.warning('请先选择一个主体')
      return
    }
    // 去重
    if (members.some((m) => m.subject_type === addType && m.subject_id === selectedId)) {
      message.warning('该成员已存在')
      return
    }
    const label = addOptions.find((o) => o.value === selectedId)?.label ?? selectedId
    setMembers([...members, { subject_type: addType, subject_id: selectedId, label }])
    setSelectedId(null)
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
        {/* 新增表单：类型 + 主体下拉选择 */}
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
            <Select
              showSearch
              optionFilterProp="label"
              placeholder={addType === 'user' ? '选择用户' : addType === 'group' ? '选择用户组' : addType === 'dept' ? '选择部门' : '选择角色'}
              value={selectedId}
              onChange={setSelectedId}
              loading={addLoading}
              notFoundContent={addLoading ? <Spin size="small" /> : '暂无可选项'}
              style={{ width: 280 }}
              options={addOptions}
            />
            <Button type="primary" onClick={addMember}>
              添加
            </Button>
          </Space>
          <div style={{ marginTop: 8 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              提示：user/group 保存其 UUID，dept 保存规整路径，role 保存角色名，均由选择自动填入
            </Text>
          </div>
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
