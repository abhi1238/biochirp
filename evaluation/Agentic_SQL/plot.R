library(readxl)
library(ggplot2)
library(patchwork)
library(scales)

# --- 1. DATA PREPARATION ---
df_r <- read_excel("~/abhi/biochirp/evaluation/Agentic_SQL/nl2SQL_result.xlsx")
df_r$Model <- gsub("groq_|openai_|google_|anthropic_", "", df_r$Model)

# Framework Abbreviations Mapping
fw_map <- c("BioChirp" = "BC", "pydanticai" = "PAI", "langchain" = "LC", 
            "crewai" = "CAI", "phidata" = "PHD")
df_r$FW_Short <- fw_map[df_r$Framework]

# Scientific Group Mapping
df_r$Group <- "OTHER"
df_r$Group[grepl("aspirin|vazalore", tolower(df_r$Question))] <- "Diseases treated by aspirin"
df_r$Group[grepl("myelogenous|myeloid", tolower(df_r$Question))] <- "DRUGS FOR CML"
df_r$Group[grepl("egfr|erbb1", tolower(df_r$Question))] <- "DRUGS TARGETTING EGFR"

df_clean <- subset(df_r, Group != "OTHER")

# Sorting Logic for Query IDs (Aligns Q-numbers with Groups)
df_clean <- df_clean[order(df_clean$Group, df_clean$Question), ]
unique_q <- unique(df_clean$Question)
df_clean$Q_ID <- factor(df_clean$Question, labels = paste0("Q", seq_along(unique_q)))

# Sort Frameworks by Latency (Fastest to Slowest)
fw_levels <- aggregate(latency ~ FW_Short, df_clean, median)
fw_levels <- fw_levels$FW_Short[order(fw_levels$latency)]
df_clean$FW_Short <- factor(df_clean$FW_Short, levels = fw_levels)

# --- 2. THE 3-COLUMN CAPTION LOGIC ---
n_q <- length(unique_q)
col_size <- ceiling(n_q / 3)
q_labels <- paste0("Q", seq_along(unique_q), ": ", unique_q)

c1 <- q_labels[1:col_size]
c2 <- q_labels[(col_size + 1):min(2 * col_size, n_q)]
c3 <- if(n_q > 2 * col_size) q_labels[(2 * col_size + 1):n_q] else ""

# Max characters per column to prevent A4 cropping (~42 chars is safe for 180mm)
max_pad <- 42 
three_col_caption <- "Reference Key:\n"

for(i in 1:col_size) {
  # Column 1 padding
  line <- sprintf(paste0("%-", max_pad, "s"), substr(c1[i], 1, max_pad - 2))
  # Column 2 padding
  if(i <= length(c2)) {
    line <- paste0(line, sprintf(paste0("%-", max_pad, "s"), substr(c2[i], 1, max_pad - 2)))
  }
  # Column 3
  if(i <= length(c3)) {
    line <- paste0(line, substr(c3[i], 1, max_pad - 2))
  }
  three_col_caption <- paste0(three_col_caption, line, "\n")
}

# --- 3. HEATMAP: SYSTEMATIC STRUCTURAL INTEGRITY ---
p1 <- ggplot(df_clean, aes(x = Q_ID, y = Model, fill = Rows)) +
  geom_tile(color = "white", linewidth = 0.2) +
  geom_text(aes(label = Rows, color = Rows == 0), size = 2.2, fontface = "bold") +
  scale_fill_viridis_c(
    option = "mako", direction = -1, trans = "pseudo_log",
    breaks = c(0, 10, 100, 1000, 9000), 
    labels = c("0", "10", "100", "1k", "9k+"),
    name = "Rows",
    guide = guide_colorbar(barheight = unit(3, "cm"), barwidth = unit(0.2, "cm"))
  ) +
  scale_color_manual(values = c("TRUE" = "#FF3030", "FALSE" = "white"), guide = "none") +
  facet_grid(FW_Short ~ Group, scales = "free", space = "free") +
  theme_minimal(base_size = 7) +
  theme(
    strip.background = element_rect(fill = "#1A1A1A", color = NA),
    strip.text = element_text(color = "white", face = "bold", size = 7.5),
    panel.spacing = unit(2, "pt"),
    axis.text.y = element_text(size = 6.5, face = "italic"),
    axis.title = element_blank(),
    panel.grid = element_blank(),
    plot.title = element_text(face = "bold", size = 10)
  ) +
  labs(title = "A: Systematic Structural Retrieval Integrity")

# --- 4. LATENCY: THE ACCURACY-TIME TRADE-OFF ---
p2 <- ggplot(df_clean, aes(x = FW_Short, y = latency, fill = FW_Short)) +
  stat_boxplot(geom = "errorbar", width = 0.1, linewidth = 0.3) +
  geom_boxplot(width = 0.4, outlier.shape = NA, alpha = 0.85, color = "black", linewidth = 0.3) +
  geom_jitter(width = 0.1, alpha = 0.2, size = 0.5) +
  # Fixed annotation placement BELOW BioChirp (BC) box
  annotate("text", x = "BC", y = 14, 
           label = "Superior Accuracy\nTrade-off", color = "#21618C", 
           size = 2.5, fontface = "bold.italic", vjust = 1) +
  scale_y_log10(limits = c(0.1, 70), 
                breaks = c(0.1, 1, 10, 30),
                labels = c("0.1", "1", "10", "30")) +
  scale_fill_manual(values = c("BC" = "#21618C", "PAI" = "#7FB3D5", "LC" = "#A9CCE3", 
                               "CAI" = "#D4E6F1", "PHD" = "#EAF2F8")) +
  theme_classic(base_size = 7) +
  theme(legend.position = "none", 
        axis.text.x = element_text(face = "bold"),
        axis.line = element_line(linewidth = 0.3)) +
  labs(title = "B: Computational Overhead vs. Model Performance", 
       y = "Latency (sec)", x = "Framework")

# --- 5. COMBINE AND SAVE FOR A4 ---
final_plot <- (p1 / p2) + 
  plot_layout(heights = c(2.5, 1)) +
  plot_annotation(
    title = "Benchmark of NL2SQL Frameworks on HCDT Database",
    subtitle = "BioChirp (BC) demonstrates consistent structural retrieval superiority across drug-target groups despite computational latency.",
    caption = three_col_caption,
    theme = theme(
      plot.title = element_text(size = 11, face = "bold"),
      plot.subtitle = element_text(size = 8.5, color = "#4D4D4D"),
      plot.caption = element_text(hjust = 0, size = 5.8, family = "mono", lineheight = 1.1)
    )
  )

# 180mm width is the standard full-page width for a Nature journal (A4)
ggsave("BioChirp_Final_A4_Publication.pdf", final_plot, 
       width = 180, height = 230, units = "mm", device = cairo_pdf)