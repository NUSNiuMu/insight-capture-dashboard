// Refresh transport defaults before creating a replacement ROS participant.
#include <fastdds/dds/domain/DomainParticipantFactory.hpp>
#include <fastdds/rtps/transport/UDPv4TransportDescriptor.h>
#include <fastdds/rtps/transport/shared_mem/SharedMemTransportDescriptor.h>
#include <memory>
#include <sstream>
#include <string>

extern "C" int insight_set_capture_addresses(const char* addresses) noexcept
{
    try {
        using namespace eprosima::fastdds;
        auto* factory = dds::DomainParticipantFactory::get_instance();
        factory->load_profiles();
        auto qos = factory->get_default_participant_qos();
        auto udp = std::make_shared<rtps::UDPv4TransportDescriptor>();
        std::istringstream input(addresses);
        std::string address;
        while (std::getline(input, address, ',')) {
            if (!address.empty()) udp->interfaceWhiteList.push_back(address);
        }
        if (udp->interfaceWhiteList.empty()) return -1;
        qos.transport().use_builtin_transports = false;
        qos.transport().user_transports.clear();
        qos.transport().user_transports.push_back(udp);
        qos.transport().user_transports.push_back(std::make_shared<rtps::SharedMemTransportDescriptor>());
        return factory->set_default_participant_qos(qos) ==
            eprosima::fastrtps::types::ReturnCode_t::RETCODE_OK ? 0 : -1;
    } catch (...) {
        return -1;
    }
}
