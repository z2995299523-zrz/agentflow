/**
 * 七步进度：每走完一步亮一格；上溯、修正会让同一步走好几遍，格子下面写次数和合计耗时
 * 正在跑时下一步是哪一步要看结果（反问、上溯、修正都会跳），这里不猜，只显示已经走完的步
 */
import { Steps } from 'antd'

import { STEPS, STEP_LABELS, type TraceStep } from '../api/types'
import { formatMs } from '../utils/format'

interface Props {
  trace: TraceStep[]
  failedStep?: string
}

export default function StepProgress({ trace, failedStep }: Props) {
  const items = STEPS.map((key) => {
    const runs = trace.filter((step) => step.step === key)
    const elapsed = runs.reduce((sum, step) => sum + step.elapsed_ms, 0)
    const status: 'finish' | 'error' | 'wait' = failedStep === key ? 'error' : runs.length ? 'finish' : 'wait'
    return {
      key,
      title: STEP_LABELS[key],
      status,
      content: runs.length ? `${formatMs(elapsed)}${runs.length > 1 ? ` · ${runs.length} 次` : ''}` : '—',
    }
  })
  return <Steps className="step-progress" size="small" titlePlacement="vertical" responsive items={items} />
}
