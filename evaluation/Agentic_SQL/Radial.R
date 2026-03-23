# 1. Setup Environment (No showtext to ensure true, searchable vector fonts)
if (!require("pacman")) install.packages("pacman")
pacman::p_load(ggplot2, dplyr, tidyr, patchwork)

# 2. Data Preparation for Spider Plot
# Spider plots require every model to have a value for every axis to close the polygon.
# We will explicitly assign 0 to frameworks where a model doesn't operate.
frameworks <- c("PydanticAI", "LangChain", "CrewAI", "PhiData", "BioChirp")
models <- c("Model-1", "Model-2", "Model-3", "Model-4")

data_raw <- list(
  "Q1" = list(agentic = c(52, 52, 52, 52), biochirp = 48, lab = "Disease:\nAspirin"),
  "Q2" = list(agentic = c(0, 0, 0, 0),     biochirp = 48, lab = "Disease:\nAspirin"),
  "Q3" = list(agentic = c(0, 0, 0, 0),     biochirp = 24, lab = "Disease:\nAspirin"),
  "Q4" = list(agentic = c(12, 12, 12, 12), biochirp = 24, lab = "Drugs:\nCML"),
  "Q5" = list(agentic = c(12, 12, 12, 12), biochirp = 24, lab = "Drugs:\nCML"),
  "Q6" = list(agentic = c(0, 0, 0, 0),     biochirp = 9855, lab = "Target:\nEGFR"),
  "Q7" = list(agentic = c(9824, 9824, 9824, 9824), biochirp = 9824, lab = "Target:\nEGFR")
)

df_list <- list()
for (q in names(data_raw)) {
  vals <- data_raw[[q]]
  
  # 1. Map Agentic Models (They get values for agentic frameworks, 0 for BioChirp framework)
  for (fw in c("PydanticAI", "LangChain", "CrewAI", "PhiData")) {
    for (i in 1:4) {
      df_list[[length(df_list) + 1]] <- data.frame(
        Question = q, Label = vals$lab, Framework = fw, Model = models[i], Value = vals$agentic[i]
      )
    }
    # BioChirp Native gets 0 on agentic frameworks
    df_list[[length(df_list) + 1]] <- data.frame(
      Question = q, Label = vals$lab, Framework = fw, Model = "BioChirp Native", Value = 0
    )
  }
  
  # 2. Map BioChirp Framework (Agentic models get 0, BioChirp Native gets its value)
  for (i in 1:4) {
    df_list[[length(df_list) + 1]] <- data.frame(
      Question = q, Label = vals$lab, Framework = "BioChirp", Model = models[i], Value = 0
    )
  }
  df_list[[length(df_list) + 1]] <- data.frame(
    Question = q, Label = vals$lab, Framework = "BioChirp", Model = "BioChirp Native", Value = vals$biochirp
  )
}

df_plot <- do.call(rbind, df_list)
# Lock factor levels to ensure correct drawing order
df_plot$Framework <- factor(df_plot$Framework, levels = frameworks)
df_plot$Model <- factor(df_plot$Model, levels = c("Model-1", "Model-2", "Model-3", "Model-4", "BioChirp Native"))

# 3. Distinct Soft Palette with Strict BioChirp Color
distinct_soft_palette <- c(
  "Model-1" = "#90CAF9", # Soft Blue
  "Model-2" = "#CE93D8", # Soft Purple
  "Model-3" = "#F48FB1", # Soft Pink
  "Model-4" = "#FFCC80", # Soft Peach
  "BioChirp Native" = "#80cbc4" # Exact Requested Matte Teal/Green
)

pt_size <- 6 / ggplot2::.pt 
base_font <- "sans" # Native vector mapping for Arial/Helvetica

# 4. Master Plotting Function: Spider Plot
create_spider <- function(q_id) {
  sub_df <- df_plot %>% filter(Question == q_id)
  max_v <- max(sub_df$Value)
  breaks_v <- seq(0, max_v, length.out = 3)
  
  # The doughnut hole keeps the center (Value=0) from becoming a messy point
  hole_offset <- -max_v * 0.25 
  
  ggplot(sub_df, aes(x = Framework, y = Value, group = Model)) +
    
    # Background Grid Setup
    geom_hline(yintercept = breaks_v[2], color = "transparent", linewidth = 0) + 
    annotate("rect", xmin = 0.5, xmax = 5.5, ymin = hole_offset, ymax = breaks_v[2], 
             fill = "#F4F6F8", alpha = 1) +
    
    # Sharp Radial Axes (The "Spider Web" Spokes)
    geom_vline(xintercept = 1:5, color = "#999999", linewidth = 0.5) +
    
    # Sharp Circular Contours (The "Spider Web" Rings)
    geom_hline(yintercept = breaks_v[2:3], color = "#AAAAAA", linewidth = 0.4, linetype = "dashed") +
    geom_hline(yintercept = max_v, color = "#666666", linewidth = 0.6, linetype = "solid") + 
    
    # SPIDER PLOT GEOMETRY: Overlapping Polygons & Vertices
    # Polygons draw the shape, points anchor the exact values on the axes
    geom_polygon(aes(fill = Model, color = Model), alpha = 0.25, linewidth = 0.5) +
    geom_point(aes(color = Model), size = 0.8) +
    
    # Numeric Labels (Positioned on the Y-axis ring)
    annotate("text", x = 0.5, y = breaks_v[-1], label = round(breaks_v[-1]), 
             size = pt_size * 0.85, family = base_font, color = "#333333", vjust = -0.4, fontface="bold") +
    
    coord_polar(clip = "off") +
    scale_fill_manual(values = distinct_soft_palette) +
    scale_color_manual(values = distinct_soft_palette) +
    
    # Expanded Y-limits to ensure 190mm width fits all labels
    scale_y_continuous(limits = c(hole_offset, max_v * 1.65)) + 
    labs(title = q_id, subtitle = unique(sub_df$Label)) +
    theme_void() + 
    theme(
      text = element_text(family = base_font, size = 6, color = "#222222"),
      plot.title = element_text(face = "bold", hjust = 0.5, size = 7, margin = margin(b=1)),
      plot.subtitle = element_text(hjust = 0.5, size = 5.5, color = "#555555"),
      
      # Framework labels explicitly at 6pt
      axis.text.x = element_text(size = 6, face = "bold", color = "#111111", vjust = -0.5),
      legend.position = "none",
      plot.margin = margin(1, 1, 1, 1, "mm")
    )
}

# 5. Assemble all 7 plots
plots <- lapply(paste0("Q", 1:7), create_spider)

final_layout <- wrap_plots(plots, nrow = 1) +
  plot_layout(guides = "collect") & 
  theme(
    legend.position = "bottom", 
    legend.title = element_blank(),
    legend.text = element_text(family = base_font, size = 6, color = "#333333"),
    legend.key = element_blank(),
    legend.key.size = unit(3, "mm"),
    legend.spacing.x = unit(2, "mm")
  ) &
  # Legend displays the polygon fill with a colored border to match the spider web
  guides(fill = guide_legend(override.aes = list(alpha = 0.5, linewidth = 0.5)))

# 6. Save as Vector PDF (Exact 190mm width, Arial/Helvetica fonts)
ggsave("spider_plot_190mm.pdf", final_layout, 
       width = 190, height = 58, units = "mm", device = cairo_pdf)

# 7. Export underlying Spider Plot dataset
write.table(df_plot, file = "spider_plot_data.txt", 
            sep = "\t", row.names = FALSE, quote = FALSE)

print("Export complete: Generated 'spider_plot_190mm.pdf'")