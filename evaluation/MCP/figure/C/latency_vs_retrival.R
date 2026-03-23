# ------------------------------------------------------------------------------
# R CODE: EFFICIENCY PLOT (SOFT MATTE NORD PALETTE + FULL AXIS)
# ------------------------------------------------------------------------------

if (!require("pacman")) install.packages("pacman")
pacman::p_load(ggplot2, dplyr, readxl, stringr, cowplot, scales, grid, extrafont)

# 1. DATA PREP
df <- read_excel("~/abhi/biochirp/evaluation/MCP/result_mcp.xlsx")

df_clean <- df %>%
  mutate(
    Count = as.numeric(as.character(Count)),
    Latency = as.numeric(as.character(Latency)),
    Error = tidyr::replace_na(Error, "Pass"),
    Type_Clean = tolower(str_trim(Type))
  ) %>%
  filter(
    tolower(Error) == "pass",
    !is.na(Count) & Count > 0,
    !is.na(Latency) & Latency > 0
  ) %>%
  mutate(
    Log_Latency = log10(Latency),
    Log_Count = log10(Count),
    
    # NORMALIZATION: Ensure 'BioChirp' model name is consistent
    Model = ifelse(grepl("biochirp", tolower(Model)), "BioChirp", Model),
    
    Style_Group = case_when(
      grepl("agentic", Type_Clean) ~ "Agentic",
      grepl("chat", Type_Clean) | grepl("claude", Type_Clean) ~ "Chat Endpoint",
      grepl("biochirp", Type_Clean) ~ "BioChirp",
      TRUE ~ "Other"
    )
  )

# Calculate Centroids
df_means <- df_clean %>%
  group_by(Model, Style_Group) %>%
  summarise(
    Mean_Log_Latency = mean(Log_Latency, na.rm = TRUE),
    Mean_Log_Count = mean(Log_Count, na.rm = TRUE),
    .groups = 'drop'
  )

# 2. LIMITS (Calculated to ensure regression lines cover 'entire axis')
x_limits <- range(df_clean$Log_Latency)
y_limits <- range(df_clean$Log_Count)

# Add 5% padding so points don't touch edges
x_padding <- diff(x_limits) * 0.05
y_padding <- diff(y_limits) * 0.05
x_limits_final <- c(x_limits[1] - x_padding, x_limits[2] + x_padding)
y_limits_final <- c(y_limits[1] - y_padding, y_limits[2] + y_padding)

# 3. STYLING & COLORS (NORD SOFT MATTE PALETTE)
custom_colors <- c(
  "BioChirp"     = "#5BBCBF", # REQUESTED TEAL (Fixed)
  
  # Soft Matte Colors (Nord Palette Inspired)
  "gpt-5-mini"   = "#BF616A", # Soft Red
  "gpt-4o-mini"  = "#D08770", # Soft Orange
  "gpt-4.1-mini" = "#EBCB8B", # Soft Yellow
  "gpt-5-nano"   = "#4C566A", # Matte Grey
  "gpt-4.1-nano" = "#B48EAD", # Soft Purple
  "Haiku 3.5"    = "#5E81AC", # Soft Blue
  "Sonnet 4.5"   = "#A3BE8C"  # Soft Green
)

custom_shapes <- c("Agentic" = 8, "Chat Endpoint" = 16, "BioChirp" = 3, "Other" = 16)
custom_linetypes <- c("Agentic" = "dashed", "Chat Endpoint" = "solid", "BioChirp" = "dotted", "Other" = "solid")

common_theme <- theme_minimal(base_family = "Arial", base_size = 7) +
  theme(
    # Clean L-Shape Axis
    panel.grid = element_blank(),
    axis.line.x.bottom = element_line(color = "black", size = 0.3),
    axis.line.y.left   = element_line(color = "black", size = 0.3),
    panel.border = element_blank(),
    axis.ticks = element_line(color = "black", size = 0.3),
    plot.margin = margin(0, 0, 0, 0)
  )

# 4. PLOT COMPONENTS

# A. Main Plot
p_main <- ggplot(df_clean, aes(x = Log_Latency, y = Log_Count, color = Model)) +
  
  # REGRESSION LINES: fullrange=TRUE ensures they cover the entire axis range
  geom_smooth(aes(linetype = Style_Group), method = "lm", se = FALSE, 
              size = 0.4, alpha = 0.6, fullrange = TRUE) +
  
  # Points
  geom_point(aes(shape = Style_Group), size = 0.8, stroke = 0.4, alpha = 0.6) +
  geom_point(data = df_means, aes(x = Mean_Log_Latency, y = Mean_Log_Count, shape = Style_Group), 
             size = 2.0, stroke = 0.6, alpha = 1.0) +
  
  # Annotation
  annotate("text", x = min(df_clean$Log_Latency), y = max(df_clean$Log_Count), 
           label = "High Yield\nFast Retrieval", hjust = 0, vjust = 1, 
           size = 2.2, color = "grey40", fontface = "italic", family = "Arial") +
  
  # Scales
  scale_shape_manual(name = "Architecture", values = custom_shapes) +
  scale_linetype_manual(name = "Architecture", values = custom_linetypes) +
  scale_color_manual(name = "Model", values = custom_colors) + 
  
  # Explicit Limits to force lines to extend fully
  scale_x_continuous(limits = x_limits_final, expand = c(0, 0)) +
  scale_y_continuous(limits = y_limits_final, expand = c(0, 0)) +
  
  labs(x = "Log10(Latency) [s]", y = "Log10(Count)") +
  common_theme +
  theme(legend.position = "none")

# B. Top Density
p_top <- ggplot(df_clean, aes(x = Log_Latency, fill = Model, color = Model)) +
  geom_density(alpha = 0.4, size = 0.2) +
  scale_fill_manual(values = custom_colors) +
  scale_color_manual(values = custom_colors) +
  scale_x_continuous(limits = x_limits_final, expand = c(0, 0)) +
  theme_void() +
  theme(legend.position = "none", plot.margin = margin(0,0,0,0))

# C. Right Density
p_right <- ggplot(df_clean, aes(x = Log_Count, fill = Model, color = Model)) +
  geom_density(alpha = 0.4, size = 0.2) +
  coord_flip() + 
  scale_fill_manual(values = custom_colors) +
  scale_color_manual(values = custom_colors) +
  scale_x_continuous(limits = y_limits_final, expand = c(0, 0)) +
  theme_void() +
  theme(legend.position = "none", plot.margin = margin(0,0,0,0))

# D. Legend
p_legend_source <- ggplot(df_clean, aes(x = Log_Latency, y = Log_Count, color = Model)) +
  geom_point(aes(shape = Style_Group), size = 2) +
  geom_line(aes(linetype = Style_Group), size = 1) +
  scale_shape_manual(name = "Architecture", values = custom_shapes) +
  scale_linetype_manual(name = "Architecture", values = custom_linetypes) +
  scale_color_manual(name = "Model", values = custom_colors) +
  theme_minimal(base_family = "Arial", base_size = 7) +
  theme(
    legend.box = "vertical",
    legend.title = element_text(face = "bold", size = 6),
    legend.text = element_text(size = 5.5),
    legend.key.size = unit(3, "mm"),
    legend.spacing = unit(1, "mm")
  )
legend <- cowplot::get_legend(p_legend_source)

# 5. LAYOUT & EXPORT
plot_grid_aligned <- cowplot::plot_grid(
  p_top, NULL, 
  p_main, p_right, 
  align = "hv", axis = "tblr", 
  rel_widths = c(4, 1), 
  rel_heights = c(1, 4),
  ncol = 2
)

final_plot <- cowplot::plot_grid(
  plot_grid_aligned, legend,
  rel_widths = c(5, 1),
  nrow = 1
)

title <- ggdraw() + 
  draw_label("Efficiency Analysis", fontfamily = "Arial", fontface = "bold", size = 8, x = 0.05, hjust = 0)

final_with_title <- plot_grid(title, final_plot, ncol = 1, rel_heights = c(0.1, 1))

ggsave("~/abhi/biochirp/evaluation/MCP/figure/C/latency_vs_retrival.pdf", plot = final_with_title, width = 100, height = 75, units = "mm", device = cairo_pdf)