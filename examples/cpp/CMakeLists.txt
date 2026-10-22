cmake_minimum_required(VERSION 3.15)
project(tinyxml2_cpp_example LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(CTest)

# The upstream unit-test sources are intentionally not part of this small fixture.
set(tinyxml2_BUILD_TESTING OFF CACHE BOOL "" FORCE)
add_subdirectory(tinyxml2)

add_executable(cpp_smoke smoke.cpp)
target_link_libraries(cpp_smoke PRIVATE tinyxml2::tinyxml2)
add_test(NAME cpp_smoke COMMAND cpp_smoke)
