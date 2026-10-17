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
  fetchParsePreview,
  KnowledgeBase,
  ParsePreview,
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

// 上传来源（P-e 溯源）：手动上传 / 目录同步 / Git 同步 / Web 源抓取
const SOURCE_TAG: Record<string, { text: string; color: string }> = {
  upload: { text: '手动上传', color: 'default' },
  sync_local: { text: '目录同步', color: 'blue' },
  sync_git: { text: 'Git 同步', color: 'purple' },
  sync_web: { text: 'Web 源', color: 'cyan' },
}

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

  // P-d：当前选中库的上传权（admin 或 库 owner），后端判定，前端只做渲染
  const selectedKbCanUpload = kbs.find((k) => k.id === selectedKb)?.can_upload ?? false

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
      title: '上传人',
      key: 'uploader',
      width: 150,
      render: (_, record) => (
        <Space size={4}>
          {/* P-e 溯源：同步/Web 来源无个人上传者，仅显示来源标识 */}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {record.uploader_name ?? '—'}
          </Typography.Text>
          {record.source_kind && SOURCE_TAG[record.source_kind] && (
            <Tag color={SOURCE_TAG[record.source_kind].color} style={{ marginInlineEnd: 0 }}>
              {SOURCE_TAG[record.source_kind].text}
            </Tag>
          )}
        </Space>
      ),
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
          {/* P-c：可见 ≠ 可删 —— 仅 admin / 库 owner / 上传者本人显示删除 */}
          {record.can_delete && (
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
          )}
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

      {selectedKbCanUpload ? (
        <Dragger
          accept=".pdf,.md,.txt,.xls,.xlsx,.docx"
          multiple
          showUploadList={false}
          disabled={uploading || !selectedKb}
        beforeUpload={(file, fileList) => {
          // INV-M-18：批量提交前校验同格式 —— antd 的 fileList 是当次拖入/
          // 选择的整批文件；混合格式整批拦截（提示只发一次），逐文件判断
          // 相同集合保证每个文件都被拦下
          const exts = new Set(fileList.map((f) => f.name.split('.').pop()?.toLowerCase() ?? ''))
          if (exts.size > 1) {
            if (fileList[0] === file) {
              message.error(`批量上传只接受同一格式，检测到 ${[...exts].join('、')}`)
            }
            return false
          }
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
      ) : (
        // P-d：无上传权（非 admin 且非库 owner）—— 仅提示，不渲染上传区
        <div
          style={{
            marginBottom: 16,
            padding: '20px 12px',
            background: '#fafafa',
            border: '1px dashed #e0e0e0',
            borderRadius: 8,
            textAlign: 'center',
          }}
        >
          <Typography.Text type="secondary">
            当前知识库仅库管理员可上传文档，如需上传请联系知识库管理员
          </Typography.Text>
        </div>
      )}

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
            // P-c：不可删行禁用勾选（批量删除仅覆盖有权行）
            getCheckboxProps: (record: Document) => ({ disabled: !record.can_delete }),
          }}
          locale={{ emptyText: <Empty description={selectedKb ? (search.trim() ? '未匹配到文档' : '该知识库暂无文档') : '请先选择知识库'} /> }}
        />
      </Card>

      <PreviewModal doc={previewDoc} onClose={() => setPreviewDoc(null)} />
    </div>
  )
}

// ─────────────────────────────────────────────────────────────
// 文档预览 Modal
//
// 两种模式（M7 WU-M-20 / INV-M-17）：
//   · 图片 / PDF → 解析预览：左侧原图（raw 接口 blob 流），右侧逐块文本化
//     结果（chunks 表真实入库内容），同屏供人工核对
//   · 其余文本系 → 现行纯文本预览（raw 接口统一提取文本）
// Modal body 高度由全局 CSS 限制在可视区内并内部滚动。
// ─────────────────────────────────────────────────────────────
/** 视觉链路支持的图片扩展（与后端 IMAGE_EXTS 同源口径，前端小写比较） */
const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp']

function PreviewModal({ doc, onClose }: { doc: Document | null; onClose: () => void }) {
  const { message } = App.useApp()
  const [loading, setLoading] = useState(false)
  const [textContent, setTextContent] = useState('')
  // 解析预览态（图片 / PDF）：预览结构 + 原图页 blob URL 列表
  const [preview, setPreview] = useState<ParsePreview | null>(null)
  const [pageUrls, setPageUrls] = useState<string[]>([])

  useEffect(() => {
    if (!doc) return
    const isVisual = doc.ext === 'pdf' || IMAGE_EXTS.includes(doc.ext.toLowerCase())
    if (!isVisual) {
      // 文本系：现行提取文本路径
      setLoading(true)
      setTextContent('')
      fetchDocumentRaw(doc.id)
        .then((r) => r.text())
        .then(setTextContent)
        .catch((e) => {
          // P-b：部分片段无权时禁止下载整份原文，给出明确提示
          if (e instanceof Error && e.message.includes('PARTIAL_DENIED')) {
            message.error('该文档包含您无权查看的内容，禁止预览原文')
          } else {
            message.error(e instanceof Error ? e.message : String(e))
          }
        })
        .finally(() => setLoading(false))
      return
    }

    // 图片 / PDF：解析预览同屏（左原图右文本）
    // <img> 无法带 Authorization 头，原图经 fetch raw → blob → objectURL
    const created: string[] = []
    setLoading(true)
    setPreview(null)
    setPageUrls([])
    fetchParsePreview(doc.id)
      .then(async (pv) => {
        setPreview(pv)
        const urls: string[] = []
        // page_raw_urls 顺序即页码 1..N，逐页 fetch raw（带 401 刷新）
        for (let i = 0; i < pv.source.page_raw_urls.length; i++) {
          const resp = await fetchDocumentRaw(doc.id, i + 1)
          urls.push(URL.createObjectURL(await resp.blob()))
        }
        created.push(...urls)
        setPageUrls(urls)
      })
      .catch((e) => message.error(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))

    return () => {
      for (const url of created) URL.revokeObjectURL(url)
    }
  }, [doc, message])

  const isVisual = doc != null && (doc.ext === 'pdf' || IMAGE_EXTS.includes(doc.ext.toLowerCase()))

  return (
    <Modal
      title={`预览 · ${doc?.filename ?? ''}`}
      open={doc != null}
      onCancel={onClose}
      footer={null}
      width={960}
    >
      <Spin spinning={loading}>
        {isVisual && preview ? (
          // 解析预览：左原图 / 右文本化结果并排（INV-M-17）
          <div style={{ display: 'flex', gap: 16, maxHeight: '62vh' }}>
            <div style={{ flex: 1, overflow: 'auto', border: '1px solid #f0f0f0', borderRadius: 8, padding: 8 }}>
              {pageUrls.map((url, i) => (
                <div key={url} style={{ marginBottom: 8 }}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    第 {i + 1} 页
                  </Typography.Text>
                  <img src={url} alt={`第 ${i + 1} 页`} style={{ width: '100%', display: 'block', borderRadius: 4 }} />
                </div>
              ))}
              {pageUrls.length === 0 && !loading && <Empty description="原图不可用" />}
            </div>
            <div style={{ flex: 1, overflow: 'auto' }}>
              {preview.blocks.length === 0 ? (
                <Empty description="尚未解析出文本（解析中或解析失败）" />
              ) : (
                preview.blocks.map((b) => (
                  <div key={b.index} style={{ marginBottom: 12 }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      块 {b.index}
                      {b.page_no != null ? ` · 第 ${b.page_no} 页` : ''}
                      {b.is_table ? ' · 表格' : ''}
                    </Typography.Text>
                    <pre
                      style={{
                        margin: '4px 0 0',
                        padding: 8,
                        background: '#fafafa',
                        borderRadius: 6,
                        whiteSpace: 'pre-wrap',
                        wordBreak: 'break-word',
                        fontSize: 12,
                        lineHeight: 1.6,
                      }}
                    >
                      {b.text}
                    </pre>
                  </div>
                ))
              )}
            </div>
          </div>
        ) : (
          textContent ? (
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
          )
        )}
      </Spin>
    </Modal>
  )
}
