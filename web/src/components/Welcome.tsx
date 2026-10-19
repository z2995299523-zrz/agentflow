/** 新会话的欢迎页：工具能做什么，以及样例库里的问法（资料里没有样例时给通用的提问建议） */
import { ApartmentOutlined, QuestionCircleOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { useEffect, useState } from 'react'

import { api } from '../api/client'
import type { ExampleInfo } from '../api/types'

const FEATURES = [
  { icon: <ApartmentOutlined />, title: '口径从血缘里来',
    text: '指标字段沿血缘上溯，带上每一跳的加工表达式、过滤条件和关联条件，同源不同口径的字段自动比对' },
  { icon: <QuestionCircleOutlined />, title: '口径有分岔先反问',
    text: '需求没说清、字段又有几种口径时先问一次，每个选项写明对应哪个字段、怎么加工' },
  { icon: <SafetyCertificateOutlined />, title: '生成后静态校验',
    text: '表和字段存在、快照表带数据日期、关联不发散……不过就带着错误重写' },
]

const TIPS = [
  '写清指标：余额、发生额、笔数、户数，要不要折人民币',
  '写清时间：某一天、月末、季末，还是一段区间的日均',
  '写清维度和范围：按机构、按行业汇总，只看某一类客户或业务',
  '一轮问完可以接着追问，比如「换成上个月末」「只看前十名」',
]

export default function Welcome({ onPick }: { onPick: (text: string) => void }) {
  const [examples, setExamples] = useState<ExampleInfo[]>([])

  useEffect(() => {
    let cancelled = false
    api.examples(6)
      .then((items) => { if (!cancelled) setExamples(items) })
      .catch(() => { if (!cancelled) setExamples([]) })
    return () => { cancelled = true }
  }, [])

  return (
    <div className="welcome">
      <div className="welcome-title">用自然语言描述查数需求</div>
      <div className="welcome-sub">
        结合专题库的数据字典、字段血缘、加工脚本和历史样例，生成可以审核的 SQL，并写清口径、用表依据和关联说明。
      </div>
      <div className="welcome-features">
        {FEATURES.map((feature) => (
          <div className="feature" key={feature.title}>
            <div className="feature-icon">{feature.icon}</div>
            <div className="feature-title">{feature.title}</div>
            <div className="feature-text">{feature.text}</div>
          </div>
        ))}
      </div>
      <div className="welcome-tips">
        {examples.length > 0 ? (
          <>
            <div className="tips-title">样例库里的问法，点一下放进输入框</div>
            <div className="suggestions">
              {examples.map((example) => (
                <button type="button" key={example.example_id} className="suggestion" onClick={() => onPick(example.requirement)}>
                  {example.requirement}
                </button>
              ))}
            </div>
          </>
        ) : (
          <>
            <div className="tips-title">怎么问更准</div>
            <ul className="tips">{TIPS.map((tip) => <li key={tip}>{tip}</li>)}</ul>
          </>
        )}
      </div>
    </div>
  )
}
