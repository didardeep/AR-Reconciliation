import { useState } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout.jsx";
import FilesPage from "./pages/FilesPage.jsx";
import RunPage from "./pages/RunPage.jsx";
import DashboardPage from "./pages/DashboardPage.jsx";
import ResultsPage from "./pages/ResultsPage.jsx";
import ExceptionsPage from "./pages/ExceptionsPage.jsx";

export default function App() {
  // The active reconciliation run is shared across pages.
  const [runId, setRunId] = useState(
    () => localStorage.getItem("ar_run_id") || null
  );
  const updateRunId = (id) => {
    if (id) localStorage.setItem("ar_run_id", id);
    setRunId(id);
  };

  return (
    <Layout runId={runId}>
      <Routes>
        <Route path="/" element={<Navigate to="/files" replace />} />
        <Route path="/files" element={<FilesPage />} />
        <Route path="/run" element={<RunPage onRun={updateRunId} />} />
        <Route path="/dashboard" element={<DashboardPage runId={runId} />} />
        <Route path="/results" element={<ResultsPage runId={runId} />} />
        <Route path="/exceptions" element={<ExceptionsPage runId={runId} />} />
      </Routes>
    </Layout>
  );
}
