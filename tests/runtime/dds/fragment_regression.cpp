// Independent wire-level regression for a partially assembled sample followed by GAP.
// Uses only localhost and a separate DDS domain; never publishes on camera topics.
#include <algorithm>
#include <arpa/inet.h>
#include <chrono>
#include <cstring>
#include <fastdds/dds/core/StackAllocatedSequence.hpp>
#include <fastdds/dds/domain/DomainParticipant.hpp>
#include <fastdds/dds/domain/DomainParticipantFactory.hpp>
#include <fastdds/dds/publisher/DataWriter.hpp>
#include <fastdds/dds/publisher/Publisher.hpp>
#include <fastdds/dds/subscriber/DataReader.hpp>
#include <fastdds/dds/subscriber/SampleInfo.hpp>
#include <fastdds/dds/subscriber/Subscriber.hpp>
#include <fastdds/dds/topic/Topic.hpp>
#include <fastdds/dds/topic/TypeSupport.hpp>
#include <fastdds/rtps/common/CDRMessage_t.h>
#include <fastdds/rtps/common/Locator.h>
#include <fastdds/rtps/common/SerializedPayload.h>
#include <fastdds/rtps/transport/UDPv4TransportDescriptor.h>
#include <fastdds/rtps/transport/test_UDPv4TransportDescriptor.h>
#include <fastrtps/attributes/LibrarySettingsAttributes.h>
#include <fastrtps/utils/IPLocator.h>
#include <fastrtps/xmlparser/XMLProfileManager.h>
#include <iostream>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
#include <vector>
using namespace eprosima::fastdds::dds;
using namespace eprosima::fastrtps::rtps;
using eprosima::fastrtps::types::ReturnCode_t;
struct Sample {
    uint32_t length = 0;
    unsigned char data[17]{};
};
class BytesType : public TopicDataType {
  public:
    BytesType() {
        setName("InsightGapRegression");
        m_typeSize = 17;
        m_isGetKeyDefined = false;
    }
    bool serialize(void *, SerializedPayload_t *) override { return false; }
    bool deserialize(SerializedPayload_t *p, void *value) override {
        auto *s = (Sample *)value;
        s->length = p->length;
        if (p->length > 17)
            return false;
        if (p->length)
            std::memcpy(s->data, p->data, p->length);
        return true;
    }
    std::function<uint32_t()> getSerializedSizeProvider(void *) override {
        return []() { return 17u; };
    }
    void *createData() override { return new Sample; }
    void deleteData(void *p) override { delete (Sample *)p; }
    bool getKey(void *, InstanceHandle_t *, bool = false) override { return false; }
};
static void need(bool b, const char *s) {
    if (!b) {
        std::cerr << "FAIL " << s << std::endl;
        std::exit(2);
    }
}
static void pause_ms(int ms) { std::this_thread::sleep_for(std::chrono::milliseconds(ms)); }
static void u16(std::vector<uint8_t> &b, uint16_t v) {
    b.push_back(v & 255);
    b.push_back(v >> 8);
}
static void u32(std::vector<uint8_t> &b, uint32_t v) {
    u16(b, v & 65535);
    u16(b, v >> 16);
}
static void seq(std::vector<uint8_t> &b, uint32_t v) {
    u32(b, 0);
    u32(b, v);
}
static void entity(std::vector<uint8_t> &b, const EntityId_t &e) {
    b.insert(b.end(), e.value, e.value + 4);
}
struct Wire {
    int fd;
    GUID_t writer, reader;
    sockaddr_in dest{};
    Wire(GUID_t w, GUID_t r) : writer(w), reader(r) {
        fd = socket(AF_INET, SOCK_DGRAM, 0);
        need(fd >= 0, "socket");
        dest.sin_family = AF_INET;
        dest.sin_port = htons(18766);
        inet_pton(AF_INET, "127.0.0.1", &dest.sin_addr);
    }
    ~Wire() { close(fd); }
    std::vector<uint8_t> header() {
        std::vector<uint8_t> b = {'R', 'T', 'P', 'S', 2, 3, 1, 15};
        b.insert(b.end(), writer.guidPrefix.value, writer.guidPrefix.value + 12);
        return b;
    }
    void send(uint8_t id, uint8_t flags, const std::vector<uint8_t> &body, uint32_t time = 0) {
        auto b = header();
        if (time) {
            b.push_back(9);
            b.push_back(1);
            u16(b, 8);
            u32(b, time);
            u32(b, 0);
        }
        b.push_back(id);
        b.push_back(flags);
        u16(b, body.size());
        b.insert(b.end(), body.begin(), body.end());
        need(sendto(fd, b.data(), b.size(), 0, (sockaddr *)&dest, sizeof(dest)) ==
                 (ssize_t)b.size(),
             "sendto");
        pause_ms(100);
    }
    void frag(uint32_t sn, uint32_t first, uint16_t count, uint32_t timestamp) {
        std::vector<uint8_t> b;
        u16(b, 0);
        u16(b, 28);
        entity(b, reader.entityId);
        entity(b, writer.entityId);
        seq(b, sn);
        u32(b, first);
        u16(b, count);
        u16(b, 8);
        u32(b, 17);
        unsigned start = (first - 1) * 8, end = std::min(17u, start + count * 8u);
        for (unsigned i = start; i < end; ++i)
            b.push_back((uint8_t)(sn * 20 + i));
        while (b.size() % 4)
            b.push_back(0);
        send(0x16, 1, b, timestamp);
    }
    void gap(uint32_t start, uint32_t base) {
        std::vector<uint8_t> b;
        entity(b, reader.entityId);
        entity(b, writer.entityId);
        seq(b, start);
        seq(b, base);
        u32(b, 0);
        send(8, 1, b);
    }
    void heartbeat(uint32_t first, uint32_t last) {
        std::vector<uint8_t> b;
        entity(b, reader.entityId);
        entity(b, writer.entityId);
        seq(b, first);
        seq(b, last);
        u32(b, 100);
        send(7, 3, b);
    }
};
int main(int argc, char **argv) {
    std::string mode = argc > 1 ? argv[1] : "gap";
    need(mode == "gap" || mode == "single" || mode == "late" || mode == "ordered" ||
             mode == "gap_only" || mode == "repeat_gap" || mode == "gap_complete",
         "mode");
    auto *f = DomainParticipantFactory::get_instance();
    eprosima::fastrtps::LibrarySettingsAttributes settings;
    settings.intraprocess_delivery = eprosima::fastrtps::IntraprocessDeliveryType::INTRAPROCESS_OFF;
    eprosima::fastrtps::xmlparser::XMLProfileManager::library_settings(settings);
    auto udp = std::make_shared<eprosima::fastdds::rtps::UDPv4TransportDescriptor>();
    udp->interfaceWhiteList.push_back("127.0.0.1");
    DomainParticipantQos pq;
    pq.transport().use_builtin_transports = false;
    pq.transport().user_transports.push_back(udp);
    auto *rp = f->create_participant(219, pq);
    auto writer_udp = std::make_shared<eprosima::fastdds::rtps::test_UDPv4TransportDescriptor>();
    writer_udp->interfaceWhiteList.push_back("127.0.0.1");
    // Discovery remains real. Only the synthetic writer's own user-data HB/GAP replies
    // are suppressed, so they cannot change the hand-crafted sequence under test.
    auto user_control = [](CDRMessage_t &msg) {
        return msg.pos + 8 <= msg.length && (msg.buffer[msg.pos + 7] & 0xc0) == 0;
    };
    writer_udp->drop_heartbeat_messages_filter_ = user_control;
    writer_udp->drop_gap_messages_filter_ = user_control;
    pq.transport().user_transports.clear();
    pq.transport().user_transports.push_back(writer_udp);
    auto *wp = f->create_participant(219, pq);
    need(wp && rp, "participants");
    TypeSupport type(new BytesType);
    need(type.register_type(wp) == ReturnCode_t::RETCODE_OK, "writer type");
    need(type.register_type(rp) == ReturnCode_t::RETCODE_OK, "reader type");
    auto *wt = wp->create_topic("insight_qa06_fragment_regression", type.get_type_name(),
                                TOPIC_QOS_DEFAULT);
    auto *rt = rp->create_topic("insight_qa06_fragment_regression", type.get_type_name(),
                                TOPIC_QOS_DEFAULT);
    auto *pub = wp->create_publisher(PUBLISHER_QOS_DEFAULT);
    auto *sub = rp->create_subscriber(SUBSCRIBER_QOS_DEFAULT);
    DataWriterQos wq;
    wq.reliability().kind = RELIABLE_RELIABILITY_QOS;
    wq.data_sharing().off();
    auto *w = pub->create_datawriter(wt, wq);
    need(w, "writer");
    DataReaderQos rq;
    rq.reliability().kind = RELIABLE_RELIABILITY_QOS;
    rq.data_sharing().off();
    rq.history().depth = mode == "late" ? 1 : 10;
    Locator_t loc;
    loc.kind = LOCATOR_KIND_UDPv4;
    loc.port = 18766;
    IPLocator::setIPv4(loc, "127.0.0.1");
    rq.endpoint().unicast_locator_list.push_back(loc);
    auto *r = sub->create_datareader(rt, rq);
    need(r, "reader");
    bool matched = false;
    for (int i = 0; i < 300; ++i) {
        SubscriptionMatchedStatus s;
        r->get_subscription_matched_status(s);
        if (s.current_count) {
            matched = true;
            break;
        }
        pause_ms(10);
    }
    need(matched, "discovery");
    pause_ms(400);
    Wire wire(w->guid(), r->guid());
    if (mode == "gap") {
        wire.frag(1, 1, 1, 100);
        wire.gap(1, 2);
        wire.frag(2, 1, 3, 200);
        wire.heartbeat(2, 2);
    } else if (mode == "gap_only") {
        wire.frag(1, 1, 1, 100);
        wire.gap(1, 2);
    } else if (mode == "repeat_gap") {
        for (unsigned i = 1; i <= 8; ++i) {
            wire.frag(i, 1, 1, i * 100);
            wire.gap(i, i + 1);
        }
        wire.frag(9, 1, 3, 900);
        wire.heartbeat(9, 9);
    } else if (mode == "gap_complete") {
        wire.frag(1, 1, 3, 100);
        wire.gap(1, 2);
        wire.frag(2, 1, 3, 200);
        wire.heartbeat(1, 2);
    } else if (mode == "single") {
        wire.frag(1, 1, 3, 100);
        wire.heartbeat(1, 1);
    } else if (mode == "late") {
        wire.frag(1, 1, 1, 100);
        wire.frag(2, 1, 3, 200);
        wire.frag(1, 2, 2, 100);
        wire.heartbeat(1, 2);
    } else {
        wire.frag(1, 1, 1, 100);
        wire.frag(1, 2, 2, 100);
        wire.frag(2, 1, 1, 200);
        wire.frag(2, 2, 2, 200);
        wire.heartbeat(1, 2);
    }
    pause_ms(200);
    unsigned count = 0, empty = 0;
    std::vector<unsigned> firstbytes;
    for (int i = 0; i < 5; ++i) {
        Sample s;
        StackAllocatedSequence<void *, 1> values;
        const_cast<void **>(values.buffer())[0] = &s;
        SampleInfoSeq infos{1};
        auto result = r->take(values, infos, 1);
        if (result == ReturnCode_t::RETCODE_NO_DATA)
            break;
        need(result == ReturnCode_t::RETCODE_OK, "take return");
        auto info = infos[0];
        if (info.valid_data) {
            ++count;
            if (!s.length)
                ++empty;
            else
                firstbytes.push_back(s.data[0]);
            std::cout << "SAMPLE length=" << s.length
                      << " source_sec=" << info.source_timestamp.seconds() << std::endl;
        }
    }
    std::vector<unsigned> expected;
    if (mode == "gap" || mode == "late")
        expected = {40};
    else if (mode == "single")
        expected = {20};
    else if (mode == "repeat_gap")
        expected = {180};
    else if (mode == "ordered" || mode == "gap_complete")
        expected = {20, 40};
    bool correct = count == expected.size() && empty == 0 && firstbytes == expected;
    std::cout << "RESULT mode=" << mode << " count=" << count << " empty=" << empty
              << " pass=" << correct << std::endl;
    rp->delete_contained_entities();
    wp->delete_contained_entities();
    f->delete_participant(rp);
    f->delete_participant(wp);
    return correct ? 0 : 1;
}
