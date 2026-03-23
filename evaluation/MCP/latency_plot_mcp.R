# Install packages if missing
if (!require("pacman")) install.packages("pacman")
pacman::p_load(readxl, ggplot2, dplyr, tidyr, stringr, scales, extrafont, ggridges)

# 1. FONT & DATA SETUP ---------------------------------------------------------
loadfonts(device = "pdf", quiet = TRUE)

# Load data (Ensure path is correct)
df <- read_excel("~/abhi/biochirp/evaluation/MCP/result_mcp.xlsx")

# Force numeric & Handle NAs
df$Latency <- suppressWarnings(as.numeric(as.character(df$Latency)))

# Data Cleaning & Feature Engineering
df <- df %>%
  filter(!is.na(Model) & Model != "") %>%
  mutate(
    Model = ifelse(tolower(Model) == "biochirp", "BioChirp", Model),
    Provider = case_when(
      Model %in% c("Haiku 3.5", "Sonnet 4.5") ~ "Claude",
      Model == "BioChirp" ~ "BioChirp",
      TRUE ~ "OpenAI"
    ),
    Type = ifelse(is.na(Type), "Unknown", Type)
  )

# Filter for valid latencies and classify workflows
df_lat <- df %>%
  filter(!is.na(Latency) & Latency > 0) %>% 
  mutate(
    Workflow = case_when(
      Provider == "OpenAI" & Type == "Agentic" ~ "Agentic",
      Provider == "OpenAI" & Type == "ChatEndPoint" ~ "Chat Endpoint",
      Model == "BioChirp" ~ "BioChirp",
      TRUE ~ "Standard" 
    )
  )

# Sort models by median latency for plotting (fastest at bottom)
lat_medians <- df_lat %>% 
  group_by(Model) %>% 
  summarise(m = median(Latency, na.rm=TRUE), .groups = "drop") %>% 
  arrange(desc(m))

df_lat$Model <- factor(df_lat$Model, levels = lat_medians$Model)

# 2. MATTE COLOR PALETTE -------------------------------------------------------
cols_matte <- c(
  "Agentic"       = "#C39BD3",  # Matte Amethyst
  "Chat Endpoint" = "#7FB3D5",  # Matte Steel Blue
  "Standard"      = "#B2BABB",  # Matte Slate Grey
  "BioChirp"      = "#5BBCBF"   # Matte Teal (BioChirp Base)
)

# 3. PLOT CONSTRUCTION (RAINCLOUD + LOG SCALE) ---------------------------------
p_g <- ggplot(df_lat, aes(x = Latency, y = Model, fill = Workflow, point_color = Workflow)) +
  
  # Raincloud Geometry: Ultra-clean separation
  geom_density_ridges(
    alpha = 0.85,                  
    color = NA,                    # No border on the distribution
    jittered_points = TRUE, 
    # Increased height to spread dots out vertically so they don't form a tight line
    position = position_points_jitter(width = 0.05, height = 0.12, yoffset = -0.22),
    point_shape = 16,              # Solid circle, no border
    point_size = 0.5,              # UPDATED: Very tiny dots
    point_alpha = 0.4,             # UPDATED: Semi-transparent to reveal overlapping density
    scale = 0.65,                  
    rel_min_height = 0.01          
  ) +
  
  # Log10 Scale for X-Axis
  scale_x_log10(
    breaks = trans_breaks("log10", function(x) 10^x),
    labels = trans_format("log10", math_format(10^.x)),
    expand = expansion(mult = c(0.05, 0.15)) 
  ) +
  
  # Apply Matte Colors
  scale_fill_manual(values = cols_matte) +
  scale_discrete_manual("point_color", values = cols_matte) + 
  
  # Labels
  labs(
    title = "Latency Distribution", 
    x = "Latency (seconds, log scale)", 
    y = NULL
  ) +
  
  # Strict 7pt Theme for 80x60mm
  theme_minimal(base_size = 7, base_family = "Arial") +
  theme(
    plot.title = element_text(face = "bold", size = 7, margin = margin(b = 4), color = "#333333"),
    axis.title = element_text(face = "bold", size = 7, color = "#555555"),
    axis.text = element_text(size = 7, color = "#333333"),
    
    # Legend settings
    legend.title = element_blank(),
    legend.text = element_text(size = 6, color = "#555555"),
    legend.position = "top",
    legend.key.size = unit(0.2, "cm"),
    legend.margin = margin(b = -5),
    
    # Clean grid setup (Dotted vertical guidelines only)
    panel.grid.major.y = element_blank(), 
    panel.grid.major.x = element_line(color = "#E0E0E0", linewidth = 0.3, linetype = "dotted"),
    panel.grid.minor = element_blank(),
    
    # Axes lines
    axis.line.x = element_line(linewidth = 0.3, color = "#999999"),
    axis.ticks.x = element_line(linewidth = 0.3, color = "#999999"),
    
    # Ultra-tight margins for physical 80x60mm size
    plot.margin = margin(2, 5, 2, 2)
  )

# 4. EXPORT --------------------------------------------------------------------
# Strictly 80mm width by 60mm height
ggsave("Latency_Raincloud_ClearDots_80x60.pdf", 
       plot = p_g, 
       width = 80, height = 60, units = "mm", 
       device = cairo_pdf)

print("✅ Saved Latency Raincloud with tiny, transparent dots for maximum clarity.")