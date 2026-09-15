import { createTheme } from "@mui/material/styles";

// Official KPMG palette
export const KPMG = {
  blue: "#00338D",       // KPMG Blue (primary)
  mediumBlue: "#005EB8",
  lightBlue: "#0091DA",
  cobalt: "#483698",     // purple accent
  teal: "#00A3A1",
  magenta: "#EC0C6E",
  green: "#43B02A",
  amber: "#EAAA00",
  ink: "#0B1F44",
  paper: "#F4F6FA",
};

// Category colors for charts (on-brand, high-contrast set)
export const CHART_COLORS = [
  KPMG.blue, KPMG.lightBlue, KPMG.cobalt, KPMG.teal,
  KPMG.magenta, KPMG.amber, KPMG.mediumBlue, KPMG.green,
];

const theme = createTheme({
  palette: {
    primary: { main: KPMG.blue, dark: "#00256A", light: KPMG.mediumBlue, contrastText: "#fff" },
    secondary: { main: KPMG.lightBlue, contrastText: "#fff" },
    success: { main: KPMG.green },
    warning: { main: KPMG.amber },
    error: { main: KPMG.magenta },
    info: { main: KPMG.mediumBlue },
    background: { default: KPMG.paper, paper: "#fff" },
    text: { primary: KPMG.ink, secondary: "#4a5878" },
  },
  shape: { borderRadius: 10 },
  typography: {
    fontFamily: '"Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    h4: { fontWeight: 700, letterSpacing: -0.3 },
    h6: { fontWeight: 700 },
    button: { textTransform: "none", fontWeight: 600 },
  },
  components: {
    MuiPaper: { styleOverrides: { root: { boxShadow: "0 1px 3px rgba(11,31,68,.08)" } } },
    MuiAppBar: { styleOverrides: { root: { backgroundColor: KPMG.blue } } },
    MuiButton: { defaultProps: { disableElevation: true } },
  },
});

export default theme;
