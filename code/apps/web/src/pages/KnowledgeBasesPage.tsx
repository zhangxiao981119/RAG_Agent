import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { App, Button, Card, Col, Empty, Row, Spin, Tag, Typography } from 'antd'
import { PlusOutlined } from '@ant-design/icons'

import { fetchKbs, KnowledgeBase } from '../mocks/data'

const { Title, Text, Paragraph } = Typography

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
                <Link to={`/knowledge-bases/${kb.id}/documents`}>
                  <Card
                    hoverable
                    variant="borderless"
                    styles={{ body: { padding: 20 } }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                      <Text strong style={{ fontSize: 15 }}>
                        {kb.name}
                      </Text>
                      {kb.is_public && <Tag color="blue">公开</Tag>}
                    </div>
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
                </Link>
              </Col>
            ))}
          </Row>
        )}
      </Spin>
    </div>
  )
}
