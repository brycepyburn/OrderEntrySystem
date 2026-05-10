# Complier + Flags
CXX = g++

# O3 --> this is for aggressive optimization; tells complier to optimize for speed
# Wall --> show all warnings
# std=c++17 --> have the modern c++ features
# Iinclude -- > look for headers in the /include folder
CXXFLAGS = -std=c++17 -O3 -Wall -Iinclude -I/opt/homebrew/include -I/usr/local/include -pthread -DCROW_USE_BOOST

# directories
SRCDIR = src
TESTDIR = tests
OBJDIR = obj
BINDIR = bin

# find all .cpp files in src/
SOURCES = $(wildcard $(SRCDIR)/*.cpp)
ENGINE_SOURCES = $(filter-out $(SRCDIR)/main.cpp,$(SOURCES))

# convert src/file.cpp into obj/fil.o
OBJECTS = $(SOURCES:$(SRCDIR)/%.cpp=$(OBJDIR)/%.o)
ENGINE_OBJECTS = $(ENGINE_SOURCES:$(SRCDIR)/%.cpp=$(OBJDIR)/%.o)
TARGET = $(BINDIR)/oes_engine
TEST_TARGET = $(BINDIR)/test_matching

# rules
all: $(TARGET)
test: $(TEST_TARGET)
	./$(TEST_TARGET)

# link objects to final binary
$(TARGET): $(OBJECTS)
	@mkdir -p $(BINDIR)
	$(CXX) $(CXXFLAGS) $(OBJECTS) -o $(TARGET)

$(TEST_TARGET): $(ENGINE_OBJECTS) $(TESTDIR)/test_matching.cpp
	@mkdir -p $(BINDIR)
	$(CXX) $(CXXFLAGS) $(ENGINE_OBJECTS) $(TESTDIR)/test_matching.cpp -o $(TEST_TARGET)

# complie each .cpp into a .o
$(OBJDIR)/%.o: $(SRCDIR)/%.cpp
	@mkdir -p $(OBJDIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

# clean build files
clean:
	rm -rf $(OBJDIR) $(BINDIR)

.PHONY: all clean test
