# Install packages if missing
if (!require("pacman")) install.packages("pacman")
pacman::p_load(readxl, ggplot2, dplyr, tidyr, stringr, scales, extrafont, ggridges)

# 1. FONT & DATA SETUP ---------------------------------------------------------
loadfonts(device = "pdf", quiet = TRUE)

# Load data (Ensure path is correct)
df <- read_excel("~/abhi/biochirp/evaluation/MCP/result_mcp.xlsx")

# Force numeric & Handle NAs
df$Latency <- suppressWarnings(as.numeric(as.character(df$Latency)))

# Data Cleaning
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

# Filter valid latencies
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

# Sort models
lat_medians <- df_lat %>% 
  group_by(Model) %>% 
  summarise(m = median(Latency, na.rm=TRUE), .groups = "drop") %>% 
  arrange(desc(m))

df_lat$Model <- factor(df_lat$Model, levels = lat_medians$Model)

# 2. MATTE COLOR PALETTE -------------------------------------------------------
cols_matte <- c(
  "Agentic"       = "#1F3C88",  
  "Chat Endpoint" = "#D55E00",  
  "Standard"      = "#B2BABB",  
  "BioChirp"      = "#5BBCBF"   
)

# 3. PLOT CONSTRUCTION (INCREASED SPACING) -------------------------------------
p_g <- ggplot(df_lat, aes(x = Latency, y = Model, fill = Workflow, point_color = Workflow)) +
  
  # Raincloud Geometry
  geom_density_ridges(
    aes(height = after_stat(ndensity)), 
    alpha = 0.85,                   
    color = NA,                    
    jittered_points = TRUE, 
    
    # Tightened jitter height (0.08) keeps dots closer to baseline
    # yoffset = -0.15 keeps them tucked just underneath
    position = position_points_jitter(width = 0.05, height = 0.08, yoffset = -0.15),
    
    point_shape = 16,              
    point_size = 0.5,             
    point_alpha = 0.4,            
    
    # KEY CHANGE: Reduced scale from 0.95 to 0.70 to create vertical gaps
    scale = 0.70,                  
    rel_min_height = 0  
  ) +
  
  # Log10 Scale for X-Axis (REMOVED PADDING)
  scale_x_log10(
    breaks = trans_breaks("log10", function(x) 10^x),
    labels = trans_format("log10", math_format(10^.x)),
    expand = c(0, 0) 
  ) +
  
  # Y-Axis
  scale_y_discrete(
    expand = expansion(add = c(0.2, 1.0)) 
  ) +
  
  # Colors
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
    
    # Grid lines (Y-Axis Grid Visible)
    panel.grid.major.y = element_line(color = "#E0E0E0", linewidth = 0.3, linetype = "dotted"), 
    panel.grid.major.x = element_line(color = "#E0E0E0", linewidth = 0.3, linetype = "dotted"),
    panel.grid.minor = element_blank(),
    
    # Axes lines (Y-Axis Line Visible)
    axis.line.x = element_line(linewidth = 0.3, color = "#999999"),
    axis.ticks.x = element_line(linewidth = 0.3, color = "#999999"),
    axis.line.y = element_line(linewidth = 0.3, color = "#999999"),
    
    plot.margin = margin(2, 5, 2, 2)
  ) +
  coord_cartesian(clip = "off") 

# 4. EXPORT --------------------------------------------------------------------
ggsave("~/abhi/biochirp/evaluation/MCP/figure/A/Latency.pdf", 
       plot = p_g, 
       width = 80, height = 60, units = "mm", 
       device = cairo_pdf)

