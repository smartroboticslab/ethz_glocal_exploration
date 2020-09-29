#ifndef GLOCAL_EXPLORATION_ROS_MAPPING_VOXBLOX_MAP_H_
#define GLOCAL_EXPLORATION_ROS_MAPPING_VOXBLOX_MAP_H_

#include <memory>
#include <string>
#include <vector>

#include <glocal_exploration/mapping/map_base.h>
#include <glocal_exploration/3rd_party/config_utilities.hpp>

#include "glocal_exploration_ros/mapping/threadsafe_wrappers/threadsafe_voxblox_server.h"

namespace glocal_exploration {
/**
 * Map class that just uses voxblox as a monolithic map baseline.
 */
class VoxbloxMap : public MapBase {
 public:
  struct Config : public config_utilities::Config<Config> {
    // Since this is a ros-class anyways we make it easy and just get the nh.
    std::string nh_private_namespace = "~";
    FloatingPoint traversability_radius = 0.3f;  // m
    FloatingPoint clearing_radius = 0.5f;        // m

    Config();
    void checkParams() const override;
    void fromRosParam() override;
  };

  explicit VoxbloxMap(const Config& config,
                      const std::shared_ptr<Communicator>& communicator);
  ~VoxbloxMap() override = default;

  /* General and Accessors */
  FloatingPoint getVoxelSize() override;
  FloatingPoint getTraversabilityRadius() override {
    return config_.traversability_radius;
  }

  /* Local planner */
  bool isTraversableInActiveSubmap(
      const Point& position,
      const FloatingPoint traversability_radius) override;
  bool isLineTraversableInActiveSubmap(
      const Point& start_point, const Point& end_point,
      const FloatingPoint traversability_radius,
      Point* last_traversable_point = nullptr) override;

  bool getDistanceAndGradientAtPositionInActiveSubmap(const Point& position,
                                                      FloatingPoint* distance,
                                                      Point* gradient) override;

  VoxelState getVoxelStateInLocalArea(const Point& position) override;
  Point getVoxelCenterInLocalArea(const Point& position) override;

  /* Global planner */
  bool isObservedInGlobalMap(const Point& position) override;
  bool isTraversableInGlobalMap(
      const Point& position,
      const FloatingPoint traversability_radius) override;
  bool isLineTraversableInGlobalMap(
      const Point& start_point, const Point& end_point,
      const FloatingPoint traversability_radius,
      Point* last_traversable_point = nullptr) override;

  std::vector<SubmapId> getSubmapIdsAtPosition(
      const Point& position) const override {
    return std::vector<SubmapId>({0u});
  }
  std::vector<SubmapData> getAllSubmapData() override;

 protected:
  const Config config_;
  std::unique_ptr<ThreadsafeVoxbloxServer> server_;

  // cached constants
  FloatingPoint c_block_size_;
  FloatingPoint c_voxel_size_;
};

}  // namespace glocal_exploration

#endif  // GLOCAL_EXPLORATION_ROS_MAPPING_VOXBLOX_MAP_H_
