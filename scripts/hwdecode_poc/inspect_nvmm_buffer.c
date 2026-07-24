#include <fcntl.h>
#include <inttypes.h>
#include <stdio.h>
#include <unistd.h>

#include <gst/allocators/gstdmabuf.h>
#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <gst/gst.h>
#include <gst/gl/gstglmemory.h>
#include <gst/video/video.h>
#include <nvbufsurface.h>

static void print_bus_error(GstElement *pipeline)
{
    GstBus *bus = gst_element_get_bus(pipeline);
    GstMessage *message = gst_bus_timed_pop_filtered(bus, 0, GST_MESSAGE_ERROR);

    if (message) {
        GError *error = NULL;
        gchar *debug = NULL;

        gst_message_parse_error(message, &error, &debug);
        fprintf(stderr, "GStreamer error: %s\n", error->message);
        if (debug)
            fprintf(stderr, "Debug: %s\n", debug);
        g_clear_error(&error);
        g_free(debug);
        gst_message_unref(message);
    }
    gst_object_unref(bus);
}

static gboolean verify_gl_import(
    GstBuffer *source_buffer, const NvBufSurfaceParams *params)
{
    GstElement *pipeline = NULL;
    GstElement *source = NULL;
    GstElement *sink = NULL;
    GstBuffer *wrapped = NULL;
    GstSample *sample = NULL;
    GstAllocator *allocator = NULL;
    GstCaps *caps = NULL;
    GstMemory *memory;
    GError *error = NULL;
    gsize offsets[GST_VIDEO_MAX_PLANES] = { 0 };
    gint strides[GST_VIDEO_MAX_PLANES] = { 0 };
    int dma_fd;
    gboolean imported = FALSE;

    dma_fd = dup((int)params->bufferDesc);
    if (dma_fd < 0) {
        perror("Could not duplicate DMA-BUF FD");
        return FALSE;
    }

    allocator = gst_dmabuf_allocator_new();
    memory = gst_dmabuf_allocator_alloc(
        allocator, dma_fd, params->dataSize);
    wrapped = gst_buffer_new();
    gst_buffer_append_memory(wrapped, memory);

    for (uint32_t plane = 0; plane < params->planeParams.num_planes; ++plane) {
        offsets[plane] = params->planeParams.offset[plane];
        strides[plane] = params->planeParams.pitch[plane];
    }
    if (!gst_buffer_add_video_meta_full(
            wrapped, GST_VIDEO_FRAME_FLAG_NONE, GST_VIDEO_FORMAT_NV12,
            params->width, params->height, params->planeParams.num_planes,
            offsets, strides)) {
        fprintf(stderr, "Could not attach GstVideoMeta to DMA-BUF\n");
        goto out;
    }
    GST_BUFFER_PTS(wrapped) = 0;
    GST_BUFFER_DURATION(wrapped) = GST_SECOND / 30;

    caps = gst_caps_new_simple(
        "video/x-raw", "format", G_TYPE_STRING, "NV12",
        "width", G_TYPE_INT, (int)params->width,
        "height", G_TYPE_INT, (int)params->height,
        "framerate", GST_TYPE_FRACTION, 30, 1, NULL);
    gst_caps_set_features(
        caps, 0, gst_caps_features_new(GST_CAPS_FEATURE_MEMORY_DMABUF, NULL));

    pipeline = gst_parse_launch(
        "appsrc name=source format=time ! glupload ! glcolorconvert ! "
        "video/x-raw(memory:GLMemory),format=RGBA ! "
        "appsink name=sink max-buffers=1 drop=true sync=false",
        &error);
    if (!pipeline) {
        fprintf(stderr, "GL import pipeline creation failed: %s\n",
            error->message);
        g_clear_error(&error);
        goto out;
    }

    source = gst_bin_get_by_name(GST_BIN(pipeline), "source");
    sink = gst_bin_get_by_name(GST_BIN(pipeline), "sink");
    gst_app_src_set_caps(GST_APP_SRC(source), caps);
    if (gst_element_set_state(pipeline, GST_STATE_PLAYING)
        == GST_STATE_CHANGE_FAILURE) {
        fprintf(stderr, "GL import pipeline could not enter PLAYING\n");
        print_bus_error(pipeline);
        goto out;
    }

    /*
     * Keep source_buffer referenced until GL upload finishes so the decoder
     * cannot recycle and overwrite the surface while the DMA-BUF is in use.
     */
    gst_buffer_ref(source_buffer);
    GstFlowReturn flow = gst_app_src_push_buffer(GST_APP_SRC(source), wrapped);
    wrapped = NULL;
    if (flow != GST_FLOW_OK) {
        fprintf(stderr, "Could not push wrapped DMA-BUF to glupload\n");
        gst_buffer_unref(source_buffer);
        goto out;
    }
    gst_app_src_end_of_stream(GST_APP_SRC(source));
    sample = gst_app_sink_try_pull_sample(
        GST_APP_SINK(sink), 5 * GST_SECOND);
    gst_buffer_unref(source_buffer);
    if (!sample) {
        fprintf(stderr, "glupload did not produce a sample\n");
        print_bus_error(pipeline);
        goto out;
    }

    GstBuffer *gl_buffer = gst_sample_get_buffer(sample);
    imported = gst_buffer_n_memory(gl_buffer) > 0
        && gst_is_gl_memory(gst_buffer_peek_memory(gl_buffer, 0));
    printf("dmabuf_to_glmemory=%s\n", imported ? "yes" : "no");

out:
    if (pipeline)
        gst_element_set_state(pipeline, GST_STATE_NULL);
    if (sample)
        gst_sample_unref(sample);
    if (wrapped)
        gst_buffer_unref(wrapped);
    if (source)
        gst_object_unref(source);
    if (sink)
        gst_object_unref(sink);
    if (pipeline)
        gst_object_unref(pipeline);
    if (caps)
        gst_caps_unref(caps);
    if (allocator)
        gst_object_unref(allocator);
    return imported;
}

int main(int argc, char **argv)
{
    GstElement *pipeline;
    GstElement *sink;
    GstSample *sample;
    GstBuffer *buffer;
    GstMapInfo map = GST_MAP_INFO_INIT;
    GError *error = NULL;
    gchar *description;
    int exit_code = 1;

    if (argc != 2) {
        fprintf(stderr, "Usage: %s <h264-mp4-file>\n", argv[0]);
        return 2;
    }

    gst_init(&argc, &argv);
    description = g_strdup_printf(
        "filesrc location=\"%s\" ! qtdemux ! h264parse ! "
        "nvv4l2decoder ! nvvidconv bl-output=false ! "
        "video/x-raw(memory:NVMM),format=NV12 ! "
        "appsink name=probe max-buffers=1 drop=true sync=false",
        argv[1]);
    pipeline = gst_parse_launch(description, &error);
    g_free(description);
    if (!pipeline) {
        fprintf(stderr, "Pipeline creation failed: %s\n", error->message);
        g_clear_error(&error);
        return 1;
    }

    sink = gst_bin_get_by_name(GST_BIN(pipeline), "probe");
    if (!sink) {
        fprintf(stderr, "Could not find probe appsink\n");
        goto out_pipeline;
    }

    if (gst_element_set_state(pipeline, GST_STATE_PLAYING)
        == GST_STATE_CHANGE_FAILURE) {
        fprintf(stderr, "Pipeline could not enter PLAYING\n");
        print_bus_error(pipeline);
        goto out_sink;
    }

    sample = gst_app_sink_try_pull_sample(GST_APP_SINK(sink), 5 * GST_SECOND);
    if (!sample) {
        fprintf(stderr, "No decoded NVMM sample within five seconds\n");
        print_bus_error(pipeline);
        goto out_sink;
    }

    buffer = gst_sample_get_buffer(sample);
    if (!gst_buffer_map(buffer, &map, GST_MAP_READ)) {
        fprintf(stderr, "Could not map the NVMM GstBuffer\n");
        goto out_sample;
    }

    NvBufSurface *surface = (NvBufSurface *)map.data;
    if (!surface->surfaceList || !surface->numFilled) {
        fprintf(stderr, "Mapped data is not a populated NvBufSurface\n");
        goto out_map;
    }

    NvBufSurfaceParams *params = &surface->surfaceList[0];
    int dma_fd = (int)params->bufferDesc;
    printf(
        "batch=%u filled=%u memory_type=%d width=%u height=%u "
        "layout=%d color=%d data_size=%u dma_fd=%d fd_valid=%s\n",
        surface->batchSize, surface->numFilled, surface->memType,
        params->width, params->height, params->layout, params->colorFormat,
        params->dataSize, dma_fd,
        fcntl(dma_fd, F_GETFD) >= 0 ? "yes" : "no");

    for (uint32_t plane = 0; plane < params->planeParams.num_planes; ++plane) {
        uint64_t modifier = params->paramex
            ? params->paramex->planeParamsex.drmModifier[plane]
            : 0;
        printf(
            "plane=%u width=%u height=%u pitch=%u offset=%u size=%u "
            "bytes_per_pixel=%u modifier=0x%016" PRIx64 "\n",
            plane, params->planeParams.width[plane],
            params->planeParams.height[plane],
            params->planeParams.pitch[plane],
            params->planeParams.offset[plane],
            params->planeParams.psize[plane],
            params->planeParams.bytesPerPix[plane], modifier);
    }
    if (!verify_gl_import(buffer, params))
        goto out_map;
    exit_code = 0;

out_map:
    gst_buffer_unmap(buffer, &map);
out_sample:
    gst_sample_unref(sample);
out_sink:
    gst_element_set_state(pipeline, GST_STATE_NULL);
    gst_object_unref(sink);
out_pipeline:
    gst_object_unref(pipeline);
    return exit_code;
}
