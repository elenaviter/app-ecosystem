// W360: info marks where the Card screen puts them, at the left and right
// edges and near the bottom, with the widget's own styles.
import { createRoot } from 'react-dom/client'

import { InfoMark } from '../../src/components/InfoMark'
import '../../src/styles.css'

const TEXT = 'Name who reviews an item in review. The operation asks for the reviewer by stable worker name, and the board records who routed it and when.'

function Row({ id, align }: { id: string; align: 'flex-start' | 'flex-end' }) {
  return (
    <div data-row={id} style={{ display: 'flex', justifyContent: align, padding: '4px 0' }}>
      <span className="tool-choice">review.assign<InfoMark text={TEXT} /></span>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
    <div>
      <Row id="top-left" align="flex-start" />
      <Row id="top-right" align="flex-end" />
    </div>
    <div>
      <Row id="bottom-left" align="flex-start" />
      <Row id="bottom-right" align="flex-end" />
    </div>
  </div>,
)
