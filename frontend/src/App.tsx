import { useState } from 'react'
import { SummaryDashboard } from './components/SummaryDashboard/SummaryDashboard'
import { TransactionFeed } from './components/TransactionFeed/TransactionFeed'

function App() {
  // Bumped after every saved disposition so the summary refetches (SCRUM-32);
  // the feed patches its own row instead.
  const [summaryRefreshKey, setSummaryRefreshKey] = useState(0)

  return (
    <>
      <SummaryDashboard refreshKey={summaryRefreshKey} />
      <TransactionFeed onDispositionSaved={() => setSummaryRefreshKey((key) => key + 1)} />
    </>
  )
}

export default App
