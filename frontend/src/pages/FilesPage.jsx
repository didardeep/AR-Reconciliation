import { useEffect, useState } from "react";
import {
  Paper, Typography, Box, Button, MenuItem, TextField, Alert, Stack,
  Table, TableHead, TableRow, TableCell, TableBody, Chip, CircularProgress,
} from "@mui/material";
import CloudUploadIcon from "@mui/icons-material/CloudUpload";
import { uploadFile, listFiles, FILE_TYPES } from "../api/client.js";

export default function FilesPage() {
  const [fileType, setFileType] = useState("sales_forward");
  const [file, setFile] = useState(null);
  const [files, setFiles] = useState([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const refresh = async () => {
    try {
      const { data } = await listFiles();
      setFiles(data);
    } catch (e) {
      setMsg({ type: "error", text: "Could not load files — is the backend running?" });
    }
  };
  useEffect(() => { refresh(); }, []);

  const onUpload = async () => {
    if (!file) return setMsg({ type: "warning", text: "Choose a spreadsheet file first." });
    setBusy(true); setMsg(null);
    try {
      await uploadFile(file, fileType);
      setMsg({ type: "success", text: `Uploaded ${file.name} as ${fileType}.` });
      setFile(null);
      await refresh();
    } catch (e) {
      const detail = e?.response?.data?.detail || e.message;
      setMsg({ type: "error", text: `Upload failed: ${detail}` });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2 }}>Files</Typography>

      <Paper sx={{ p: 3, mb: 3 }}>
        <Typography variant="h6" sx={{ mb: 2 }}>Upload an Excel file</Typography>
        <Stack direction={{ xs: "column", md: "row" }} spacing={2} alignItems="center">
          <TextField
            select label="File type" value={fileType}
            onChange={(e) => setFileType(e.target.value)} sx={{ minWidth: 280 }}
          >
            {FILE_TYPES.map((f) => <MenuItem key={f.value} value={f.value}>{f.label}</MenuItem>)}
          </TextField>

          <Button variant="outlined" component="label" startIcon={<CloudUploadIcon />}>
            {file ? file.name : "Choose file (.xlsx/.xlsb/.xls/.xlsm)"}
            <input hidden type="file" accept=".xlsx,.xlsb,.xls,.xlsm"
                   onChange={(e) => setFile(e.target.files?.[0] || null)} />
          </Button>

          <Button variant="contained" onClick={onUpload} disabled={busy}>
            {busy ? <CircularProgress size={22} color="inherit" /> : "Upload"}
          </Button>
        </Stack>
        {msg && <Alert severity={msg.type} sx={{ mt: 2 }}>{msg.text}</Alert>}
      </Paper>

      <Paper sx={{ p: 3 }}>
        <Typography variant="h6" sx={{ mb: 2 }}>Uploaded files</Typography>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>ID</TableCell><TableCell>Filename</TableCell>
              <TableCell>Type</TableCell><TableCell>Status</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {files.map((f) => (
              <TableRow key={f.id} hover>
                <TableCell>{f.id}</TableCell>
                <TableCell>{f.filename}</TableCell>
                <TableCell><Chip size="small" label={f.file_type} /></TableCell>
                <TableCell>{f.processing_status}</TableCell>
              </TableRow>
            ))}
            {files.length === 0 && (
              <TableRow><TableCell colSpan={4}>
                <Typography color="text.secondary" variant="body2">No files uploaded yet.</Typography>
              </TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </Paper>
    </Box>
  );
}
