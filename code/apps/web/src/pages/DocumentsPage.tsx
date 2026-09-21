import React, { useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  App,
  Button,
  Card,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Progress,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  Upload,
} from 'antd'
import { DeleteOutlined, EyeOutlined, InboxOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'

import {
  deleteDocument,
  Document,
  fetchDocuments,
  fetchDocumentRaw,
  fetchKbs,
  KnowledgeBase,
  uploadDocument,
} from '../mocks/data'

const { Title, Text } = Typography
const { Dragger } = Upload

// 文档解析状态 → 标签颜色
const STATUS_TAG: Record<Document['status'], { text: string; color: string }> = {
  pending: { text: '待处理', color: 'default' },
  parsing: { text: '解析中', color: 'processing' },
  indexed: { text: '已索引', color: 'success' },
  failed: { text: '处理失败', color: 'error' },
}

const LEVEL_LABEL: Record<number, string> = { 10: '公开', 20: '内部', 30: '机密', 40: '绝密' }

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function DocumentsPage() {
  const { message } = App.useApp()
  const { kbId } = useParams<{ kbId?: string }>()
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [selectedKb, setSelectedKb] = useState<string>(kbId || '')
  const [documents, setDocuments] = useState<Document[]>([])
  const [uploading, setUploading] = useState(false)
  /** 上传进度百分比，null 表示当前没有文件在上传 */
  const [uploadProgress, setUploadProgress] = useState<number | null>(null)
  const [previewDoc, setPreviewDoc] = useState<Document | null>(null)
  /** 前端搜索关键字（按文件名过滤） */
  const [search, setSearch] = useState('')
  /** Table rowSelection 选中行 id */
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([])

  // documents 的 ref 镜像：供轮询 effect 读取最新数组，避免把 documents 放进依赖导致定时器反复重建
  const documentsRef = useRef<Document[]>([])
  documentsRef.current = documents

  useEffect(() => {
    fetchKbs()
      .then((list) => {
        setKbs(list)
        if (list.length > 0) setSelectedKb((prev) => prev || list[0].id)
      })
      .catch((e) => message.error(e instanceof Error ? e.message : String(e)))
  }, [message])

  useEffect(() => {
    if (kbId) setSelectedKb(kbId)
  }, [kbId])

  // 切换知识库时清空选中行（避免跨库残留）
  useEffect(() => {
    setSelectedRowKeys([])
  }, [selectedKb])

  useEffect(() => {
    if (!selectedKb) return
    fetchDocuments(selectedKb)
      .then(setDocuments)
      .catch((e) => message.error(e instanceof Error ? e.message : String(e)))
  }, [selectedKb, message])

  // 轮询：有 pending/parsing 文档时每 3s 刷新（手册 C6 验收）
  // 用 ref 读取 documents 最新值，避免把 documents 数组放进依赖 → 定时器因引用变化反复销毁重建
  useEffect(() => {
    if (!selectedKb) return
    const timer = setInterval(() => {
      const current = documentsRef.current
      const hasParsing = current.some(
        (d) => d.status === 'pending' || d.status === 'parsing',
      )
      if (!hasParsing) return
      fetchDocuments(selectedKb).then(setDocuments).catch(() => undefined)
    }, 3000)
    return () => clearInterval(timer)
  }, [selectedKb])

  async function handleUpload(file: File) {
    if (!selectedKb) return
    setUploading(true)
    setUploadProgress(0)
    try {
      await uploadDocument(selectedKb, file, (percent) => setUploadProgress(percent))
      message.success(`文件「${file.name}」已上传，等待解析`)
      setDocuments(await fetchDocuments(selectedKb))
    } catch (e) {
      message.error(e instanceof Error ? e.message : '上传失败')
    } finally {
      setUploading(false)
      setUploadProgress(null)
    }
  }

  async function handleBatchDelete(ids: React.Key[]) {
    const uuidList = ids.map((k) => String(k))
    let ok = 0
    let fail = 0
    await Promise.allSettled(uuidList.map((id) => deleteDocument(id))).then((results) => {
      results.forEach((r) => {
        if (r.status === 'fulfilled') ok++
        else fail++
      })
    })
    setDocuments((prev) => prev.filter((d) => !ids.includes(d.id)))
    setSelectedRowKeys([])
    if (fail === 0) {
      message.success(`已删除 ${ok} 个文档`)
    } else {
      message.warning(`删除 ${ok} 个成功，${fail} 个失败`)
    }
  }

  async function handleDelete(doc: Document) {
    try {
      await deleteDocument(doc.id)
      message.success('文档已删除')
      setDocuments((prev) => prev.filter((d) => d.id !== doc.id))
    } catch (e) {
      message.error(e instanceof Error ? e.message : '删除失败')
    }
  }

  // 前端搜索过滤：按文件名匹配（大小写不敏感），空关键字返回全部
  const filteredDocuments = documents.filter((d) => {
    const q = search.trim().toLowerCase()
    if (!q) return true
    return d.filename.toLowerCase().includes(q)
  })

  const columns: ColumnsType<Document> = [
    { title: '文件名', dataIndex: 'filename', key: 'filename', ellipsis: true },
    {
      title: '大小',
      dataIndex: 'size_bytes',
      key: 'size_bytes',
      width: 110,
      render: (v: number) => formatSize(v),
    },
    {
      title: '密级',
      dataIndex: 'level_rank',
      key: 'level_rank',
      width: 90,
      render: (v: number) => LEVEL_LABEL[v] || '内部',
    },
    {
      title: '版本',
      dataIndex: 'version',
      key: 'version',
      width: 80,
      render: (v: number) => `v${v}`,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (v: Document['status']) => <Tag color={STATUS_TAG[v].color}>{STATUS_TAG[v].text}</Tag>,
    },
    {
      title: '上传时间',
      dataIndex: 'uploaded_at',
      key: 'uploaded_at',
      width: 170,
      render: (v: string) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm') : '—'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 160,
      render: (_, record) => (
        <Space size={4}>
          <Button
            type="link"
            size="small"
            icon={<EyeOutlined />}
            onClick={() => setPreviewDoc(record)}
          >
            预览
          </Button>
          <Popconfirm
            title={`确定删除文档「${record.filename}」吗？`}
            description="删除后不可恢复。"
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => handleDelete(record)}
          >
            <Button type="link" size="small" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20, flexWrap: 'wrap', gap: 12 }}>
        <div>
          <Title level={4} style={{ margin: 0 }}>
            文档
          </Title>
          <Text type="secondary">管理知识库中的文档与索引状态</Text>
        </div>
        <Select
          style={{ width: 280 }}
          placeholder="选择知识库"
          value={selectedKb || undefined}
          onChange={setSelectedKb}
          options={kbs.map((kb) => ({ label: kb.name, value: kb.id }))}
          showSearch
          optionFilterProp="label"
        />
      </div>

      <Dragger
        accept=".pdf,.md,.txt,.xls,.xlsx,.docx"
        showUploadList={false}
        disabled={uploading || !selectedKb}
        beforeUpload={(file) => {
          void handleUpload(file)
          // 返回 false 阻止 antd Upload 自动发请求，上传逻辑由 uploadDocument 接管
          return false
        }}
        style={{ marginBottom: 16 }}
      >
        <p className="ant-upload-drag-icon">
          <InboxOutlined />
        </p>
        <p className="ant-upload-text">{uploading ? '正在上传...' : '点击或拖放文件到此处上传'}</p>
        <p className="ant-upload-hint">支持 PDF / Markdown / TXT / XLS / XLSX / DOCX，单文件不超过 100 MB</p>
      </Dragger>

      {uploading && uploadProgress != null && (
        <div style={{ marginBottom: 16, padding: '8px 12px', background: '#f5f5f5', borderRadius: 6 }}>
          <Progress percent={uploadProgress} status={uploadProgress >= 100 ? 'success' : 'active'} size="small" />
        </div>
      )}

      <Card variant="borderless" styles={{ body: { padding: 16 } }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, gap: 12, flexWrap: 'wrap' }}>
          {selectedRowKeys.length > 0 ? (
            <Popconfirm
              title={`确定批量删除选中的 ${selectedRowKeys.length} 个文档吗？`}
              description="删除后不可恢复。"
              okText="全部删除"
              okButtonProps={{ danger: true }}
              cancelText="取消"
              onConfirm={() => void handleBatchDelete(selectedRowKeys)}
            >
              <Button danger icon={<DeleteOutlined />}>
                批量删除 ({selectedRowKeys.length})
              </Button>
            </Popconfirm>
          ) : (
            <div />
          )}
          <Input.Search
            placeholder="按文件名搜索"
            allowClear
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ width: 240 }}
          />
        </div>
        <Table<Document>
          rowKey="id"
          columns={columns}
          dataSource={filteredDocuments}
          size="middle"
          pagination={{ pageSize: 10, showSizeChanger: true, showQuickJumper: true }}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys: React.Key[]) => setSelectedRowKeys(keys),
          }}
          locale={{ emptyText: <Empty description={selectedKb ? (search.trim() ? '未匹配到文档' : '该知识库暂无文档') : '请先选择知识库'} /> }}
        />
      </Card>

      <PreviewModal doc={previewDoc} onClose={() => setPreviewDoc(null)} />
    </div>
  )
}

// ─────────────────────────────────────────────────────────────
// 文档原文预览 Modal
//
// 后端 raw 接口已把所有上传格式统一提取为纯文本（md/txt 原文；
// docx/xlsx/xls/pdf 由后端提取），前端统一按文本展示。
// Modal body 高度由全局 CSS 限制在可视区内并内部滚动。
// ─────────────────────────────────────────────────────────────
function PreviewModal({ doc, onClose }: { doc: Document | null; onClose: () => void }) {
  const { message } = App.useApp()
  const [loading, setLoading] = useState(false)
  const [textContent, setTextContent] = useState('')

  useEffect(() => {
    if (!doc) return
    setLoading(true)
    setTextContent('')
    fetchDocumentRaw(doc.id)
      .then((r) => r.text())
      .then(setTextContent)
      .catch((e) => message.error(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [doc, message])

  return (
    <Modal
      title={`预览 · ${doc?.filename ?? ''}`}
      open={doc != null}
      onCancel={onClose}
      footer={null}
      width={860}
    >
      <Spin spinning={loading}>
        {textContent ? (
          doc?.ext === 'md' ? (
            <div className="md-body" style={{ fontSize: 13 }}>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{textContent}</ReactMarkdown>
            </div>
          ) : (
            <pre
              style={{
                margin: 0,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                fontFamily: 'inherit',
                fontSize: 13,
                lineHeight: 1.7,
              }}
            >
              {textContent}
            </pre>
          )
        ) : (
          !loading && <Empty description="暂无内容" />
        )}
      </Spin>
    </Modal>
  )
}
