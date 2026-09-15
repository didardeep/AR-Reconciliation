import { useLocation, useNavigate } from "react-router-dom";
import {
  AppBar, Toolbar, Typography, Box, Drawer, List, ListItemButton,
  ListItemIcon, ListItemText, Chip, Container,
} from "@mui/material";
import UploadFileIcon from "@mui/icons-material/UploadFile";
import PlayCircleIcon from "@mui/icons-material/PlayCircle";
import DashboardIcon from "@mui/icons-material/Dashboard";
import TableChartIcon from "@mui/icons-material/TableChart";
import ReportProblemIcon from "@mui/icons-material/ReportProblem";
import { KPMG } from "../theme.js";

const NAV = [
  { to: "/files", label: "Files", icon: <UploadFileIcon /> },
  { to: "/run", label: "Run Reconciliation", icon: <PlayCircleIcon /> },
  { to: "/dashboard", label: "Dashboard", icon: <DashboardIcon /> },
  { to: "/results", label: "Results", icon: <TableChartIcon /> },
  { to: "/exceptions", label: "Exceptions", icon: <ReportProblemIcon /> },
];
const DRAWER_W = 248;

export default function Layout({ children, runId }) {
  const nav = useNavigate();
  const loc = useLocation();

  return (
    <Box sx={{ display: "flex", minHeight: "100vh" }}>
      <AppBar position="fixed" sx={{ zIndex: (t) => t.zIndex.drawer + 1 }}>
        <Toolbar>
          <Box
            sx={{
              px: 1.2, py: 0.4, mr: 2, bgcolor: "#fff", color: KPMG.blue,
              fontWeight: 800, letterSpacing: 1, borderRadius: 1, fontSize: 20,
            }}
          >
            KPMG
          </Box>
          <Typography variant="h6" sx={{ flexGrow: 1, fontWeight: 600 }}>
            AR Reconciliation — Sales vs Settlement
          </Typography>
          <Chip
            size="small"
            label={runId ? `Active run #${runId}` : "No active run"}
            sx={{ bgcolor: runId ? KPMG.lightBlue : "rgba(255,255,255,.25)", color: "#fff" }}
          />
        </Toolbar>
      </AppBar>

      <Drawer
        variant="permanent"
        sx={{
          width: DRAWER_W, flexShrink: 0,
          [`& .MuiDrawer-paper`]: { width: DRAWER_W, boxSizing: "border-box" },
        }}
      >
        <Toolbar />
        <List sx={{ pt: 1 }}>
          {NAV.map((n) => {
            const active = loc.pathname === n.to;
            return (
              <ListItemButton
                key={n.to}
                selected={active}
                onClick={() => nav(n.to)}
                sx={{
                  mx: 1, my: 0.3, borderRadius: 2,
                  "&.Mui-selected": { bgcolor: "rgba(0,51,141,.10)" },
                  "&.Mui-selected .MuiListItemIcon-root": { color: KPMG.blue },
                }}
              >
                <ListItemIcon sx={{ minWidth: 40 }}>{n.icon}</ListItemIcon>
                <ListItemText
                  primary={n.label}
                  primaryTypographyProps={{ fontWeight: active ? 700 : 500 }}
                />
              </ListItemButton>
            );
          })}
        </List>
      </Drawer>

      <Box component="main" sx={{ flexGrow: 1, bgcolor: "background.default" }}>
        <Toolbar />
        <Container maxWidth="xl" sx={{ py: 3 }}>
          {children}
        </Container>
      </Box>
    </Box>
  );
}
